"""x264/x265 enrichment of FFmpeg's libx264/libx265 codec options.

FFmpeg declares ``-preset`` / ``-tune`` / ``-profile`` on these codecs as
``AV_OPT_TYPE_STRING`` and forwards the value verbatim, so the AVOption
parser sees no enumerated values. We pin the upstream library to an
approximately-contemporary snapshot (commit for x264, stable tag for x265)
at or before the FFmpeg release date, parse its CLI help, and layer the
recovered value lists + richer descriptions onto the FFmpeg-side codec
entries via :func:`layer_upstream_string_values`.

The two libraries differ only in the snapshot-resolution scheme, the file
set to parse, and one render kwarg (x265's commercial-licensing notice);
:func:`_enrich_upstream` runs both through one orchestrator driven by
project-specific hooks bundled in :class:`_UpstreamProject`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ._logging import Logger
from .attribution import DOC_TO_ROOT, NOTICES_FILENAME, THIRD_PARTY, emit_upstream_doc
from .git_utils import commit_at_or_before, show_file, tag_at_or_before
from .models import ExtractConfig
from .upstream_help import HelpDoc, UpstreamOptionHelp, render_help_doc
from .x264_help import parse_x264_doc
from .x265_help import parse_x265_doc

X264_FAMILY = frozenset({"libx264", "libx264rgb", "libx262"})
X265_FAMILY = frozenset({"libx265"})

# Pre-compiled regex matching x265's stable release tags only — pre-release
# tags (``3.5_RC1``, ``3.5_RC2``) would otherwise win the date comparison
# over the corresponding stable release and pin the snapshot to incomplete
# preset/tune logic. Accepts ``MAJOR.MINOR`` and ``MAJOR.MINOR.PATCH``.
_X265_STABLE_TAG = re.compile(r"\d+\.\d+(\.\d+)?")


# Per-worker parse caches for the upstream ``HelpDoc`` structures, keyed
# by snapshot identity (x264 commit SHA / x265 tag). Module-level so they
# persist across tags handled by the same worker. With
# ``ProcessPoolExecutor`` each worker has its own copy — cross-worker
# reuse would need a Manager, not worth it for ~50ms of parse work; the
# on-disk file-existence check already captures cross-worker overlap.
_x264_parse_cache: dict[str, HelpDoc] = {}
_x265_parse_cache: dict[str, HelpDoc] = {}


def layer_upstream_string_values(
    codec: dict,
    help_map: dict[str, UpstreamOptionHelp],
    family: frozenset[str],
    source_label: str,
) -> None:
    """Overlay upstream library help onto a codec's options.

    Some libraries — notably x264 and x265 — accept their own
    ``-preset`` / ``-tune`` / ``-profile`` as opaque strings that FFmpeg
    forwards verbatim. They also document many of their other options
    (``--crf``, ``--qp``, ``--aq-mode``, …) in their CLI help, often
    with information FFmpeg's terse texi doesn't include (default
    values, ranges, per-value semantics).

    For every FFmpeg option whose bare name (e.g. ``crf`` from ``-crf``)
    matches an entry in ``help_map``:

    - **Value list**: filled from ``info.values`` when the FFmpeg option
      currently has none. This is the preset/tune/profile case.
    - **Description**: ``info.description`` is appended to the FFmpeg
      option's description list as a clearly-attributed markdown
      paragraph. Skipped if the same line is already present (idempotent
      across re-extractions).

    Pure name-equality matching — no hardcoded translation table. Options
    whose FFmpeg spelling diverges from the upstream CLI spelling (e.g.
    FFmpeg's ``-coder`` vs x264's ``--no-cabac``) simply pass through
    unenriched.

    ``family`` is the lowercase set of codec names this map applies to
    (e.g. ``{"libx264", "libx264rgb", "libx262"}``). ``source_label``
    appears in the rendered attribution (``**From upstream x264:** ...``).
    """
    if not help_map:
        return
    names = {codec.get("name", "").lower(), *(
        a.lower() for a in codec.get("aliases", [])
    )}
    if not names & family:
        return
    attribution_prefix = f"**From upstream {source_label}:**"
    for opt in codec.get("options", []):
        bare = opt["name"][1:] if opt["name"].startswith("-") else opt["name"]
        info = help_map.get(bare)
        if info is None:
            continue
        # Value-list overlay (preset/tune/profile case).
        if info.values and not opt.get("values"):
            opt["values"] = [name for name, _ in info.values]
            opt["valueDescriptions"] = [desc for _, desc in info.values]
        # Description overlay: append the upstream header text as an
        # extra markdown paragraph, clearly attributed so the reader
        # knows which source it came from. De-duped by attribution
        # prefix so repeated runs don't pile up identical lines.
        if info.description:
            existing = list(opt.get("description") or [])
            if not any(p.startswith(attribution_prefix) for p in existing):
                existing.append(f"{attribution_prefix} {info.description}")
                opt["description"] = existing


@dataclass(frozen=True)
class _UpstreamProject:
    """Per-project hooks driving :func:`_enrich_upstream`.

    Bundles every callable that differs between x264 and x265: how the
    snapshot is pinned to the FFmpeg release date, how the snapshot id is
    displayed (commit SHA truncated to 12 hex vs the tag as-is), how the
    source files are read and parsed, how the parsed help is summarized
    for the debug log, and any extra kwargs for the page renderer.
    """

    project: str  # "x264" / "x265"
    identifier_kind: str  # "commit" / "tag"
    # Pin a snapshot id (commit SHA, tag name) at or before the given
    # ISO-8601 date, or ``None`` if no suitable snapshot exists.
    resolve_snapshot: Callable[[Path, str], "str | None"]
    # Map a raw snapshot id to the form displayed in logs and used as the
    # web path slug (e.g. ``commit[:12]`` for x264, the tag itself for x265).
    display_identifier: Callable[[str], str]
    # Pull source files at the snapshot and run the project's parser.
    # Returns ``None`` when the main CLI source is missing (and logs its
    # own warning); returns a possibly-empty :class:`HelpDoc` otherwise —
    # the empty case is handled uniformly by the caller.
    fetch_and_parse: Callable[[Path, str, Logger], "HelpDoc | None"]
    # One-line summary of the parsed help map's richness for the debug log.
    log_richness: Callable[[dict[str, UpstreamOptionHelp]], str]
    # Module-level per-worker parse cache for this project.
    cache: dict[str, HelpDoc]
    # Extra kwargs forwarded to :func:`render_help_doc` (e.g. x265's
    # commercial-licensing notice). Defaults to nothing.
    extra_render_kwargs: dict[str, str] = field(default_factory=dict)


def _fetch_and_parse_x264(repo: Path, commit: str, logger: Logger) -> HelpDoc | None:
    x264_c_text = show_file(repo, commit, "x264.c")
    if x264_c_text is None:
        logger.warn(
            f"x264 enrichment skipped: x264.c not found at commit {commit[:12]}"
        )
        return None
    # Optional auxiliary sources so the parser can resolve printf-style
    # placeholders (``%d``, ``%.1f``, …) in the descriptions to actual
    # constants and default values. Each is best-effort: missing → that
    # resolution path is just skipped, the rest still works.
    base_c = show_file(repo, commit, "common/base.c") or ""
    common_h = show_file(repo, commit, "common/common.h") or ""
    x264_h = show_file(repo, commit, "x264.h") or ""
    return parse_x264_doc(x264_c_text, base_c=base_c, common_h=common_h, x264_h=x264_h)


def _fetch_and_parse_x265(repo: Path, tag: str, logger: Logger) -> HelpDoc | None:
    def src(path: str) -> str:
        return show_file(repo, tag, path) or ""
    return parse_x265_doc(
        src("source/x265cli.cpp"),
        src("source/common/param.cpp"),
        src("source/encoder/level.cpp"),
        common_h=src("source/common/common.h"),
        x265_h=src("source/x265.h"),
    )


def _x264_log_richness(help_map: dict[str, UpstreamOptionHelp]) -> str:
    with_values = sum(1 for v in help_map.values() if v.values)
    with_desc = sum(1 for v in help_map.values() if v.description)
    return (
        f"{len(help_map)} options "
        f"({with_values} with value lists, {with_desc} with descriptions)"
    )


def _x265_log_richness(help_map: dict[str, UpstreamOptionHelp]) -> str:
    with_desc = sum(1 for v in help_map.values() if v.description)
    return f"{len(help_map)} options ({with_desc} with descriptions)"


_X264_PROJECT = _UpstreamProject(
    project="x264",
    identifier_kind="commit",
    resolve_snapshot=commit_at_or_before,
    display_identifier=lambda commit: commit[:12],
    fetch_and_parse=_fetch_and_parse_x264,
    log_richness=_x264_log_richness,
    cache=_x264_parse_cache,
)


_X265_PROJECT = _UpstreamProject(
    project="x265",
    identifier_kind="tag",
    resolve_snapshot=lambda repo, date: tag_at_or_before(
        repo, date, _X265_STABLE_TAG
    ),
    display_identifier=lambda tag: tag,
    fetch_and_parse=_fetch_and_parse_x265,
    log_richness=_x265_log_richness,
    cache=_x265_parse_cache,
    extra_render_kwargs={
        "commercial_notice": THIRD_PARTY["x265"]["commercial"],
    },
)


def _enrich_upstream(
    project_spec: _UpstreamProject,
    repo: Path,
    released: str | None,
    out: Path,
    ffmpeg_repo: Path,
    logger: Logger,
) -> tuple[dict[str, UpstreamOptionHelp], str]:
    """Resolve, parse, cache, log, and render an upstream library's help.

    Returns ``(help_map, doc_path)``. ``help_map`` is the by-name option
    index the codec-overlay step consumes; ``doc_path`` is the SPA
    web-relative path to the rendered reference page (empty when no
    page was produced — caller omits the index.json key).
    """
    name = project_spec.project
    kind = project_spec.identifier_kind

    if not released:
        logger.warn(f"{name} enrichment skipped: FFmpeg tag has no committer date")
        return {}, ""

    snapshot = project_spec.resolve_snapshot(repo, released)
    if snapshot is None:
        logger.warn(
            f"{name} enrichment skipped: no {name} {kind} at or before {released}"
        )
        return {}, ""

    display = project_spec.display_identifier(snapshot)

    cached = project_spec.cache.get(snapshot)
    if cached is not None:
        struct = cached
        logger.debug(f"{name} cache hit for {kind} {display}")
    else:
        struct = project_spec.fetch_and_parse(repo, snapshot, logger)
        if struct is None:
            return {}, ""
        project_spec.cache[snapshot] = struct

    help_map = struct.options
    if not help_map:
        logger.warn(
            f"{name} help empty at {kind} {display}; "
            f"lib{name} -preset/-tune/-profile won't get values"
        )
        return {}, ""

    logger.debug(
        f"Parsed {name} help from {kind} {display} (<= {released}): "
        f"{project_spec.log_richness(help_map)}"
    )

    info = THIRD_PARTY[name]
    html_content = render_help_doc(
        struct,
        project=name,
        identifier=display,
        identifier_kind=kind,
        source_url=info["source_url"],
        license_name=info["license"],
        license_href=f"{DOC_TO_ROOT}/licenses/{info['license_file']}",
        notices_href=f"{DOC_TO_ROOT}/{NOTICES_FILENAME}",
        copyright_line=info["copyright"],
        **project_spec.extra_render_kwargs,
    )
    doc_path = emit_upstream_doc(
        out, ffmpeg_repo, name, display, html_content, logger,
    )
    return help_map, doc_path


def enrich_x264(
    config: ExtractConfig, released: str | None, logger: Logger,
) -> tuple[dict[str, UpstreamOptionHelp], str]:
    """Run x264 enrichment for one FFmpeg tag. Returns ``({}, "")`` when
    ``--x264-repo`` wasn't supplied or no snapshot could be resolved."""
    if config.x264_repo is None:
        return {}, ""
    return _enrich_upstream(
        _X264_PROJECT, config.x264_repo, released, config.out, config.repo, logger,
    )


def enrich_x265(
    config: ExtractConfig, released: str | None, logger: Logger,
) -> tuple[dict[str, UpstreamOptionHelp], str]:
    """Run x265 enrichment for one FFmpeg tag. Returns ``({}, "")`` when
    ``--x265-repo`` wasn't supplied or no snapshot could be resolved."""
    if config.x265_repo is None:
        return {}, ""
    return _enrich_upstream(
        _X265_PROJECT, config.x265_repo, released, config.out, config.repo, logger,
    )
