from __future__ import annotations

import concurrent.futures
import contextlib
import html
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from .avopt_c import (
    AVCODEC_GLOBAL_KEY,
    AVFORMAT_GLOBAL_KEY,
    ParsedOption,
    build_class_options_map,
    enrich_options_with_c_values,
)
from .config_texi import generate_config_texi
from .git_utils import (
    commit_at_or_before,
    list_tags,
    show_file,
    show_file_bytes,
    tag_at_or_before,
    tag_date_iso,
    temporary_worktree,
)
from .models import AVOptionEntry, ExtractConfig
from .upstream_help import (
    HelpDoc,
    UpstreamOptionHelp,
    build_generated_doc_footer,
    render_help_doc,
)
from .x264_help import parse_x264_doc
from .x265_help import parse_x265_doc

# Pre-compiled regex matching x265's stable release tags only — pre-release
# tags (``3.5_RC1``, ``3.5_RC2``) would otherwise win the date comparison
# over the corresponding stable release and pin the snapshot to incomplete
# preset/tune logic. Accepts ``MAJOR.MINOR`` and ``MAJOR.MINOR.PATCH``.
_X265_STABLE_TAG = re.compile(r"\d+\.\d+(\.\d+)?")
from .parsing import (
    dedupe_av_options,
    dedupe_codecs,
    dedupe_filters,
    dedupe_named,
    dedupe_options,
    merge_codec_flags,
    merge_per_codec_options,
    parse_bitstream_filters_xml,
    parse_codec_options_xml,
    parse_codecs_c,
    parse_codecs_xml,
    parse_demuxers_xml,
    parse_filters_xml,
    parse_format_options_xml,
    parse_muxers_xml,
    parse_options_xml,
    parse_per_codec_options_xml,
    parse_per_format_options_xml,
    parse_protocols_xml,
)
from .texi_xml import MakeinfoError, resolve_makeinfo, run_makeinfo, run_makeinfo_html

_TAG_PATTERN = re.compile(r"^n(\d+)\.(\d+)\.(\d+)$")

# HTML rendering assets (t2h.pm + the two CSS files) are fetched from this
# pinned FFmpeg tag at build time rather than vendored into the package, so no
# GPL-licensed bytes (t2h.pm is "part of FFmpeg", GPLv3+) are committed to this
# MIT repo — consistent with the project rule that GPL-derived artifacts are
# generated at build time and never checked in. The tag is pinned (not "the
# tag being rendered") because n8.1.1's t2h.pm is version-gated for Texinfo
# 7.1+, while older tags' copies call APIs (e.g. $self->gdt) that 7.1 removed;
# the n8.1.1 init file is the one that renders across the whole tag range.
_PINNED_ASSET_TAG = "n8.1.1"
_SHARED_CSS_FILES = ("bootstrap.min.css", "style.min.css")
_T2H_DOC_PATH = "doc/t2h.pm"

# n8.1.1's doc/t2h.pm emits two stylesheet <link>s pointing at CSS sitting
# beside the generated HTML. We repoint each one directory up so every
# version's page under doc/ffmpeg/<version>/ shares a single CSS pair at
# doc/ffmpeg/. This is the only modification made to the upstream file, applied
# here in code (MIT) rather than to a committed copy. Each replacement must hit
# exactly once; a miss means the pinned init file changed shape and the repoint
# silently no-opped, so we skip HTML with a loud warning instead.
_T2H_HREF_REPOINTS = (
    ('href="bootstrap.min.css"', 'href="../bootstrap.min.css"'),
    ('href="style.min.css"', 'href="../style.min.css"'),
)

# Per-process cache of pinned-tag asset bytes keyed by repo-relative path
# (``None`` = confirmed absent). Pool workers are separate processes, so each
# shells out to ``git show`` at most once per asset.
_pinned_asset_cache: dict[str, bytes | None] = {}

# --- Third-party license / attribution -------------------------------------
#
# The distributed artifacts (per-version JSON, the FFmpeg ``ffmpeg-all.html``
# reference, and the x264/x265 reference pages) are derivative works of GPL /
# LGPL upstreams, so the deploy must carry each upstream's license text, a
# copyright notice, and a pointer to the corresponding source. We fetch the
# verbatim ``COPYING`` file from each repo at build time into ``<out>/licenses``
# (never vendored — keeps this repo's tree 100% MIT) and emit one aggregate
# ``THIRD-PARTY-NOTICES.html`` at the output root.
#
# FFmpeg is consumed only via its documentation + ``libav*`` headers (none of
# the GPL-only files), so the LGPL v2.1 text is the governing license; x264 and
# x265 both ship the GPL v2 text as ``COPYING`` and are "v2 or later".
_FFMPEG_LICENSE_SRC = "COPYING.LGPLv2.1"
_UPSTREAM_LICENSE_SRC = "COPYING"

# Relative prefix from a rendered doc page (always 3 levels deep:
# ``doc/ffmpeg/<ver>/``, ``doc/x264/<id>/``, ``doc/x265/<id>/``) back to the
# output root, where ``licenses/`` and ``THIRD-PARTY-NOTICES.html`` live.
_DOC_TO_ROOT = "../../.."
_NOTICES_FILENAME = "THIRD-PARTY-NOTICES.html"

# Static descriptor for each upstream, keyed by the slug used in output paths.
# ``license_file`` is the name written under ``<out>/licenses/``.
_THIRD_PARTY = {
    "ffmpeg": {
        "title": "FFmpeg",
        "license": "GNU LGPL v2.1 or later",
        "license_file": "LICENSE_FFMPEG.txt",
        "copyright": "Copyright (c) the FFmpeg developers",
        "source_url": "https://github.com/FFmpeg/FFmpeg",
        "derived_from": "the FFmpeg documentation and libav* headers",
    },
    "x264": {
        "title": "x264",
        "license": "GNU GPL v2 or later",
        "license_file": "LICENSE_X264.txt",
        "copyright": "Copyright (c) the x264 project",
        "source_url": "https://code.videolan.org/videolan/x264",
        "derived_from": "the x264 command-line help text and source",
    },
    "x265": {
        "title": "x265",
        "license": "GNU GPL v2 or later",
        "license_file": "LICENSE_X265.txt",
        "copyright": "Copyright (c) MulticoreWare, Inc. and contributors",
        "source_url": "https://bitbucket.org/multicoreware/x265_git",
        "derived_from": "the x265 command-line help text and source",
        "commercial": "x265 is also available under a commercial license; "
        "contact license@x265.com.",
    },
}


def _pinned_asset_bytes(repo: Path, path: str) -> bytes | None:
    """Return ``path`` at :data:`_PINNED_ASSET_TAG` from ``repo``, memoized."""
    if path not in _pinned_asset_cache:
        _pinned_asset_cache[path] = show_file_bytes(repo, _PINNED_ASSET_TAG, path)
    return _pinned_asset_cache[path]


# Log fan-in queue for parallel workers. ``None`` in the parent and in
# sequential runs (where the single process writes to the console directly);
# set in pool workers via :func:`_pool_initializer`. When set, a worker ships
# each log line to the parent instead of writing it itself, and the parent's
# drain thread is the *sole* writer to the console.
#
# A cross-process lock is not enough here: it serializes the Python-level
# ``write()`` call, but on Windows several processes writing to the same
# console handle still render torn — the visible symptom was raw ``\\r\\n``
# bytes mid-line shown as ``♪◙`` (CP437 for CR/LF) under ``--jobs > 1``.
# Funnelling every line back to the single parent writer eliminates that:
# only one process ever touches the console, exactly as in sequential mode.
_log_queue = None

# Serializes the parent's own console writes (failure/summary lines emitted
# from the main thread) against the queue-drain thread — both run in the
# parent process during parallel extraction. Unused in workers.
_console_lock = threading.Lock()

# Cross-process lock that serializes upstream-library reference HTML
# writes (x264 + x265). Several FFmpeg tags often pin the same upstream
# snapshot (e.g. n8.1 and n8.1.1 both → x264 0480cb05fa18) and without
# this lock concurrent workers would race to write the same
# ``doc/<project>/<id>/<project>-reference.html``. Held only during the
# render+write inside :func:`_emit_upstream_doc`, with a check-existing
# fast path so the second worker reuses the first's output for free.
_upstream_doc_lock = None

# Per-worker parse caches for the upstream ``HelpDoc`` structures, keyed
# by snapshot identity (x264 commit SHA / x265 tag). Module-level so they
# persist across tags handled by the same worker. With
# ``ProcessPoolExecutor`` each worker has its own copy — cross-worker
# reuse would need a Manager, not worth it for ~50ms of parse work; the
# on-disk file-existence check already captures cross-worker overlap.
_x264_parse_cache: "dict[str, object]" = {}
_x265_parse_cache: "dict[str, object]" = {}

# Per-worker memo of ``project:identifier`` snapshots this worker has
# already emitted (or confirmed on disk). Lets the second tag pinning the
# same snapshot skip even the lock acquisition + file-stat round-trip.
_upstream_emit_cache: "set[str]" = set()


def _pool_initializer(log_queue, upstream_doc_lock) -> None:
    """Runs once per worker process at pool start-up. Stashes the shared log
    queue and upstream-doc lock in module-level slots so every Logger emit
    ships to the parent and every upstream doc write in this worker is
    serialized."""
    global _log_queue, _upstream_doc_lock
    _log_queue = log_queue
    _upstream_doc_lock = upstream_doc_lock


def _upstream_emit_context():
    """Context manager wrapping the upstream-doc emit phase with the
    shared cross-process lock when installed (parallel mode), or a no-op
    otherwise (sequential mode — no race possible)."""
    return (
        _upstream_doc_lock
        if _upstream_doc_lock is not None
        else contextlib.nullcontext()
    )


def _emit_to_console(stream: str, line: str) -> None:
    """Write one line to the real console as a single ``write()+flush()``.

    Only ever called in the parent (or in a sequential run) — the
    ``_console_lock`` serializes the drain thread against the main thread.
    """
    target = sys.stderr if stream == "stderr" else sys.stdout
    with _console_lock:
        target.write(line + "\n")
        target.flush()


def _write_line(stream: str, line: str) -> None:
    """Emit a log line. In a pool worker (``_log_queue`` set) the line is
    shipped to the parent's drain thread; otherwise it goes straight to the
    console. This keeps a single process as the sole console writer."""
    if _log_queue is not None:
        _log_queue.put((stream, line))
    else:
        _emit_to_console(stream, line)


def _drain_log_queue(log_queue) -> None:
    """Parent-side worker: write queued worker log lines to the console until
    the sentinel (``None``) arrives. The single console writer for pooled mode."""
    while True:
        item = log_queue.get()
        if item is None:
            return
        stream, line = item
        _emit_to_console(stream, line)


class Logger:
    """Per-tag log emitter.

    ``tag`` (when set) is prepended to every line as ``[{tag}] `` so output
    from concurrently-extracting workers stays attributable. Writes happen
    immediately — no buffering — so users see progress as it streams. In
    pooled mode each line is shipped to the parent's drain thread (the sole
    console writer) via ``_log_queue``.
    """

    def __init__(self, verbose: bool, *, tag: str | None = None) -> None:
        self._verbose = verbose
        self._tag = tag

    def _emit(self, stream: str, message: str) -> None:
        line = f"[{self._tag}] {message}" if self._tag else message
        _write_line(stream, line)

    def info(self, message: str) -> None:
        self._emit("stdout", message)

    def debug(self, message: str) -> None:
        if self._verbose:
            self._emit("stdout", message)

    def warn(self, message: str) -> None:
        self._emit("stderr", f"WARNING: {message}")


def _extract_for_tag_pooled(
    config: ExtractConfig, tag: str, makeinfo_cmd: list[str]
) -> bool:
    """Worker entry point for ``--jobs > 1`` extraction.

    The worker ships its log lines to the parent's drain thread via the queue
    set up in :func:`_pool_initializer`, so users see streaming progress while
    a single process remains the sole console writer. Returns ``True`` on
    success, ``False`` after warning about the failure — the parent only needs
    the boolean to track failures.
    """
    logger = Logger(config.verbose, tag=tag)
    try:
        _extract_for_tag(config, tag, makeinfo_cmd, logger)
        return True
    except ExtractionError as exc:
        logger.warn(f"Extraction failed: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — surface every failure mode
        logger.warn(f"Worker crashed: {type(exc).__name__}: {exc}")
        return False


class ExtractionError(Exception):
    pass


def parse_tag_version(tag: str) -> tuple[int, int, int] | None:
    match = _TAG_PATTERN.match(tag)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def semver_key(tag: str) -> tuple[int, int, int]:
    version = parse_tag_version(tag)
    if version is None:
        return (0, 0, 0)
    return version


def select_tags(config: ExtractConfig, logger: Logger) -> list[str]:
    tags = list_tags(config.repo)
    tags = [t for t in tags if parse_tag_version(t)]

    if config.tags:
        selected = [t for t in config.tags if t in tags]
        missing = [t for t in config.tags if t not in tags]
        if missing:
            raise ExtractionError(f"Tags not found: {', '.join(missing)}")
    elif config.tag_range:
        start, end = config.tag_range
        start_v = parse_tag_version(start)
        end_v = parse_tag_version(end)
        if start_v is None or end_v is None:
            raise ExtractionError("Range tags must match n<major>.<minor>.<patch>")
        selected = [t for t in tags if start_v <= parse_tag_version(t) <= end_v]  # type: ignore
        if not selected:
            raise ExtractionError("No tags found within range")
    else:
        selected = tags

    selected.sort(key=semver_key)

    if not config.latest_per_minor:
        return selected

    latest: dict[tuple[int, int], str] = {}
    for tag in selected:
        major, minor, patch = semver_key(tag)
        key = (major, minor)
        current = latest.get(key)
        if current is None or semver_key(tag) > semver_key(current):
            latest[key] = tag

    result = sorted(latest.values(), key=semver_key)
    logger.debug(f"Selected tags: {', '.join(result)}")
    return result


def _stage_subtrees(repo: Path, tag: str, dest: Path, subtrees: tuple[str, ...]) -> bool:
    """Materialize the given subtrees of ``tag`` into ``dest``. Returns
    True iff every subtree extracts and the first one — historically
    ``doc/`` — is present afterward.

    Uses ``git archive`` to avoid touching the working tree. The
    available-subtree-set differs across older tags (e.g. libavformat
    moved files around); a missing optional subtree is logged by the
    caller, not failed here, so callers should only check existence of
    the doc subtree (which is the hard requirement).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", tag, *subtrees],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    try:
        # Pipe the tar stream into ``tar -x`` inside ``dest``. We rely on the
        # tar that ships with both Git for Windows and Unix systems.
        tar = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            input=proc.stdout,
            capture_output=True,
            check=True,
        )
        _ = tar
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return (dest / subtrees[0]).is_dir()


def _stage_doc_dir(repo: Path, tag: str, dest: Path) -> bool:
    """Compat wrapper — stage ``doc/`` plus the libav source trees we
    use for AVOption parsing. ``libavcodec``/``libavformat`` are
    best-effort: their absence on a given tag will silently disable
    C-source value enrichment, not the whole extraction."""
    return _stage_subtrees(repo, tag, dest, ("doc", "libavcodec", "libavformat"))


def _write_dummy_config_texi(repo: Path, tag: str, doc_dir: Path, version: str) -> None:
    """Write a generated ``config.texi`` into ``doc_dir`` if absent.

    The real file is produced by ``./configure`` at build time; we synthesize
    a feature-everything-enabled stand-in by parsing ``configure``. This
    lets ``makeinfo`` resolve ``@include config.texi`` and evaluate every
    ``@ifset config-…`` conditional in the docs.
    """
    target = doc_dir / "config.texi"
    if target.exists():
        return
    configure_text = show_file(repo, tag, "configure")
    if not configure_text:
        return
    target.write_text(
        generate_config_texi(configure_text, version),
        encoding="utf-8",
    )


@contextmanager
def _staged_doc(repo: Path, tag: str, version: str, fallback_root: Path | None):
    """Yield a directory containing ``doc/`` for ``tag``.

    Tries ``git archive`` against ``repo`` first. Falls back to the
    pre-extracted worktree ``fallback_root`` if provided. A synthetic
    ``config.texi`` is written into the staged ``doc/`` so makeinfo can
    resolve ``@include config.texi`` without running ``./configure``.
    """
    if fallback_root is not None and (fallback_root / "doc").is_dir():
        _write_dummy_config_texi(repo, tag, fallback_root / "doc", version)
        yield fallback_root
        return

    tmp = Path(tempfile.mkdtemp(prefix="ffmpeg-doc-"))
    try:
        if not _stage_doc_dir(repo, tag, tmp):
            raise ExtractionError(f"Could not stage doc/ for tag {tag}")
        _write_dummy_config_texi(repo, tag, tmp / "doc", version)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _load_xml(
    doc_root: Path,
    filename: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    cache: dict[str, ET.Element | None] | None = None,
) -> ET.Element | None:
    """Render ``doc/<filename>`` via ``makeinfo --xml`` and return the root.

    A ``cache`` dict (per-tag) memoizes results so the same ``.texi`` file
    isn't re-rendered when multiple extraction steps consume it (e.g.
    ``codecs.texi`` is read for both ``codecs`` and ``codec_options``;
    ``muxers.texi`` / ``demuxers.texi`` are each read twice as well). The
    cache stores ``None`` for missing/unparseable sources so a second
    lookup doesn't re-emit the warning.
    """
    if cache is not None and filename in cache:
        return cache[filename]
    src = doc_root / "doc" / filename
    if not src.exists():
        result: ET.Element | None = None
    else:
        try:
            result = run_makeinfo(src, cwd=src.parent, cmd=makeinfo_cmd)
        except MakeinfoError as exc:
            logger.warn(f"makeinfo failed on {filename}: {exc}")
            result = None
    if cache is not None:
        cache[filename] = result
    return result


def _load_text(repo: Path, tag: str, path: str, fallback_root: Path | None) -> str | None:
    content = show_file(repo, tag, path)
    if content is not None:
        return content
    if fallback_root is None:
        return None
    file_path = fallback_root / path
    if file_path.exists():
        return file_path.read_text(encoding="utf-8", errors="ignore")
    return None


def _extract_options(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    for source in ("ffmpeg.texi", "ffmpeg-all.texi", "ffmpeg-opt.texi"):
        root = _load_xml(doc_root, source, makeinfo_cmd, logger, xml_cache)
        if root is None:
            continue
        logger.debug(f"Parsing options from {source}")
        options = dedupe_options(parse_options_xml(root))
        return [
            {
                "name": o.name,
                "aliases": o.aliases,
                "scope": o.scope,
                "valueType": o.value_type,
                "values": o.values,
                "requires": o.requires,
                "conflicts": o.conflicts,
                "description": o.description,
                "anchor": o.anchor,
                "signature": o.signature,
            }
            for o in options
        ]
    raise ExtractionError("Options sources not found")


_X264_FAMILY = frozenset({"libx264", "libx264rgb", "libx262"})
_X265_FAMILY = frozenset({"libx265"})


def _layer_upstream_string_values(
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


def _extract_codecs(
    doc_root: Path,
    repo: Path,
    tag: str,
    fallback_root: Path | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
    x264_help: dict[str, UpstreamOptionHelp],
    x265_help: dict[str, UpstreamOptionHelp],
) -> list[dict]:
    root = _load_xml(doc_root, "codecs.texi", makeinfo_cmd, logger, xml_cache)
    codecs_from_doc = dedupe_codecs(parse_codecs_xml(root)) if root is not None else []

    codec_flags: dict[str, dict[str, bool]] = {}
    for source in ("libavcodec/allcodecs.c", "libavcodec/codec_list.c"):
        text = _load_text(repo, tag, source, fallback_root)
        if text:
            logger.debug(f"Parsing codec flags from {source}")
            codec_flags.update(parse_codecs_c(text))

    if not codecs_from_doc and not codec_flags:
        raise ExtractionError("Codec sources not found")

    # Merge: doc gives type + aliases for documented codecs, allcodecs.c
    # gives the comprehensive name list with encoder/decoder flags. Names
    # only present in allcodecs.c surface as type="video" (the historical
    # default — we don't try to infer type from the symbol name).
    merged = merge_codec_flags(codecs_from_doc, codec_flags)
    seen = {c.name for c in merged}
    for alias_set in (c.aliases for c in codecs_from_doc):
        seen.update(alias_set)

    extras = []
    for name, flags in sorted(codec_flags.items()):
        if name in seen:
            continue
        extras.append(
            {
                "name": name,
                "type": "video",
                "aliases": [],
                "encoder": flags.get("encoder", False),
                "decoder": flags.get("decoder", False),
                "anchor": "",
            }
        )

    documented = [
        {
            "name": c.name,
            "type": c.type,
            "aliases": c.aliases,
            "encoder": c.encoder,
            "decoder": c.decoder,
            "anchor": c.anchor,
        }
        for c in merged
    ]
    codecs = sorted(documented + extras, key=lambda c: c["name"])

    # Attach per-codec private options harvested from encoders.texi /
    # decoders.texi. Both sources are optional; older tags' docs may be
    # missing one or both, in which case the per-codec options[] just
    # stays empty on every entry.
    known_names: set[str] = set()
    for c in codecs:
        known_names.add(c["name"])
        for alias in c.get("aliases", []):
            known_names.add(alias)

    encoder_options: dict[str, list[AVOptionEntry]] = {}
    decoder_options: dict[str, list[AVOptionEntry]] = {}
    enc_root = _load_xml(doc_root, "encoders.texi", makeinfo_cmd, logger, xml_cache)
    if enc_root is not None:
        logger.debug("Parsing per-codec encoder options from encoders.texi")
        encoder_options = parse_per_codec_options_xml(enc_root, "encoder", known_names)
    dec_root = _load_xml(doc_root, "decoders.texi", makeinfo_cmd, logger, xml_cache)
    if dec_root is not None:
        logger.debug("Parsing per-codec decoder options from decoders.texi")
        decoder_options = parse_per_codec_options_xml(dec_root, "decoder", known_names)

    per_codec = merge_per_codec_options(encoder_options, decoder_options)
    for c in codecs:
        # Aliases share an options table — when ``parse_codecs_xml`` collapsed
        # ``libx264, libx264rgb`` into a single entry, the per-codec parser
        # may have keyed its options under either name. Try the canonical
        # first, then any aliases, and emit the first hit.
        opts: list[AVOptionEntry] = per_codec.get(c["name"], [])
        if not opts:
            for alias in c.get("aliases", []):
                opts = per_codec.get(alias, [])
                if opts:
                    break
        # Overlay AV_OPT_TYPE_CONST value descriptions from the C source.
        # The lookup tries the canonical codec name plus all aliases; the
        # first AVClass-bound match wins.
        enriched = enrich_options_with_c_values(
            opts, c_map, [c["name"], *c.get("aliases", [])]
        )
        c["options"] = [_av_option_to_dict(o) for o in enriched]
        # Fill values+descriptions for libx264/libx265's string-typed
        # passthrough options (-preset / -tune / -profile) from the
        # upstream library sources. Both calls are no-ops for codecs
        # outside their respective family.
        _layer_upstream_string_values(c, x264_help, _X264_FAMILY, "x264")
        _layer_upstream_string_values(c, x265_help, _X265_FAMILY, "x265")

    return codecs


def _av_option_to_dict(o) -> dict:
    # ``value_descriptions`` is normalized to len(values) before serialization
    # so the SPA can pair them index-by-index without bounds checks.
    descs = list(o.value_descriptions)
    if len(descs) < len(o.values):
        descs.extend([""] * (len(o.values) - len(descs)))
    elif len(descs) > len(o.values):
        descs = descs[: len(o.values)]
    return {
        "name": o.name,
        "aliases": o.aliases,
        "valueType": o.value_type,
        "values": o.values,
        "valueDescriptions": descs,
        "description": o.description,
        "anchor": o.anchor,
        "signature": o.signature,
        "roles": o.roles,
    }


def _extract_codec_options(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Parse the generic AVCodec options chapter from ``codecs.texi``.

    Missing/unparseable source returns an empty list — older tags' docs may
    not carry the chapter, and we don't want to fail extraction over it.
    The SPA tolerates an absent ``codec_options`` array.
    """
    root = _load_xml(doc_root, "codecs.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.debug("codecs.texi not found; codec_options will be empty")
        return []
    logger.debug("Parsing codec_options from codecs.texi")
    options = dedupe_av_options(parse_codec_options_xml(root))
    options = enrich_options_with_c_values(options, c_map, [AVCODEC_GLOBAL_KEY])
    return [_av_option_to_dict(o) for o in options]


def _attach_per_format_options(
    entries: list[dict],
    doc_root: Path,
    source_file: str,
    side: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> None:
    """Enrich a list of muxer/demuxer dicts in place with an ``options`` field
    sourced from ``muxers.texi`` / ``demuxers.texi``.

    Missing source: every entry's ``options`` becomes an empty list. The SPA
    tolerates absence anyway, so older bundles aren't affected.
    """
    known: set[str] = set()
    for e in entries:
        known.add(e["name"])
        for alias in e.get("aliases", []):
            known.add(alias)

    by_name: dict[str, list[AVOptionEntry]] = {}
    root = _load_xml(doc_root, source_file, makeinfo_cmd, logger, xml_cache)
    if root is not None:
        logger.debug(f"Parsing per-{side} options from {source_file}")
        by_name = parse_per_format_options_xml(root, side, known)

    for e in entries:
        opts: list[AVOptionEntry] = by_name.get(e["name"], [])
        if not opts:
            for alias in e.get("aliases", []):
                opts = by_name.get(alias, [])
                if opts:
                    break
        enriched = enrich_options_with_c_values(
            opts, c_map, [e["name"], *e.get("aliases", [])]
        )
        e["options"] = [_av_option_to_dict(o) for o in enriched]


def _extract_format_options(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Parse the generic AVFormat options chapter from ``formats.texi``."""
    root = _load_xml(doc_root, "formats.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.debug("formats.texi not found; format_options will be empty")
        return []
    logger.debug("Parsing format_options from formats.texi")
    options = dedupe_av_options(parse_format_options_xml(root))
    options = enrich_options_with_c_values(options, c_map, [AVFORMAT_GLOBAL_KEY])
    return [_av_option_to_dict(o) for o in options]


def _extract_filters(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    root = _load_xml(doc_root, "filters.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        raise ExtractionError("Filter sources not found")
    logger.debug("Parsing filters from filters.texi")
    filters = dedupe_filters(parse_filters_xml(root))
    return [
        {
            "name": f.name,
            "type": f.type,
            "aliases": f.aliases,
            "params": f.params,
            "description": f.description,
            "args": f.args,
        }
        for f in filters
    ]


def _extract_named(
    doc_root: Path,
    source_file: str,
    parser,
    category: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Run ``parser`` against ``doc_root/doc/<source_file>`` and serialize.

    Returns an empty list (and warns) if the source texi is missing, so a
    single unavailable catalog doesn't fail the whole extraction. Hard
    parse/makeinfo failures still surface as warnings via ``_load_xml``.
    """
    root = _load_xml(doc_root, source_file, makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.warn(f"{category} source not found ({source_file})")
        return []
    logger.debug(f"Parsing {category} from {source_file}")
    entries = dedupe_named(parser(root))
    return [
        {
            "name": e.name,
            "aliases": e.aliases,
            "anchor": e.anchor,
            "description": e.description,
        }
        for e in entries
    ]


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _build_index(
    version: str,
    released: str | None,
    categories: set[str],
    *,
    x264_doc: str = "",
    x265_doc: str = "",
) -> dict:
    index: dict = {
        "version": version,
        "released": released or "",
    }
    # Only advertise files this run actually produced. Older bundles on disk
    # carried only options/codecs/filters; the SPA must tolerate the absence
    # of the value-lookup keys, which is exactly what omitting them signals.
    if "options" in categories:
        index["options"] = "options.json"
    if "codecs" in categories:
        index["codecs"] = "codecs.json"
    if "filters" in categories:
        index["filters"] = "filters.json"
    if "demuxers" in categories:
        index["demuxers"] = "demuxers.json"
    if "muxers" in categories:
        index["muxers"] = "muxers.json"
    if "protocols" in categories:
        index["protocols"] = "protocols.json"
    if "bitstream_filters" in categories:
        index["bitstream_filters"] = "bitstream_filters.json"
    # Path is relative to the SPA's public root (``web/public/``), not
    # to this index.json's directory — the file lives in a sibling tree
    # (``doc/x264/<commit>/``) keyed on x264 commit so multiple FFmpeg
    # versions pinning the same x264 commit share one rendered file.
    # Absent when --x264-repo wasn't supplied for this run.
    if x264_doc:
        index["x264_doc"] = x264_doc
    if x265_doc:
        index["x265_doc"] = x265_doc
    return index


def _extract_for_tag(
    config: ExtractConfig, tag: str, makeinfo_cmd: list[str], logger: Logger
) -> None:
    version = parse_tag_version(tag)
    if version is None:
        raise ExtractionError(f"Invalid tag format: {tag}")

    major, minor, patch = version
    target_version = f"{major}.{minor}"
    released = tag_date_iso(config.repo, tag)

    try:
        _extract_and_write(config, tag, target_version, released, makeinfo_cmd, logger, None)
        return
    except ExtractionError as exc:
        if not config.worktree_fallback:
            raise
        logger.warn(f"Primary extraction failed for {tag}: {exc}")

    try:
        with temporary_worktree(config.repo, tag) as root:
            _extract_and_write(
                config, tag, target_version, released, makeinfo_cmd, logger, root
            )
    except Exception as exc:
        raise ExtractionError(f"Worktree fallback failed: {exc}")


def _extract_and_write(
    config: ExtractConfig,
    tag: str,
    target_version: str,
    released: str | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    fallback_root: Path | None,
) -> None:
    output_dir = config.out / "metadata" / "ffmpeg" / target_version
    logger.info(f"Extracting {tag} -> {output_dir}")

    with _staged_doc(config.repo, tag, target_version, fallback_root) as doc_root:
        # AVOption value descriptions come from the libav* C source, which
        # is staged alongside ``doc/`` by ``_stage_doc_dir``. Missing trees
        # (very old tag, archive failure) collapse to an empty map — the
        # texi-derived options still surface, just without enum descriptions.
        c_map = build_class_options_map(
            (doc_root / "libavcodec", doc_root / "libavformat")
        )
        if c_map:
            logger.debug(f"Parsed AVOption tables for {len(c_map)} classes")
        else:
            logger.debug("No AVOption tables parsed from libav* sources")

        # Memoize ``makeinfo --xml`` output per filename for this tag —
        # several .texi files are parsed twice (codecs.texi for codecs +
        # codec_options; muxers.texi / demuxers.texi each for the catalog
        # and the per-entity options pass) and re-running ``makeinfo`` is
        # the bulk of per-tag wall time. ``None`` is cached too so a
        # repeated lookup doesn't re-emit the makeinfo-failed warning.
        xml_cache: dict[str, ET.Element | None] = {}

        # Upstream x264 help text — pinned to the x264 commit at or
        # before this FFmpeg tag's release date so older bundles get an
        # approximately-contemporary preset/tune/profile set rather than
        # today's HEAD. x264 carries no tags or release branches, so the
        # commit date is the only signal we have. Parsing is cheap
        # (~5ms); we just re-parse per tag rather than thread a shared
        # map through the worker pool.
        #
        # ``x264_doc_path`` is the relative web path of the per-commit
        # x264 HTML reference (when rendered). It surfaces in this tag's
        # ``index.json`` so the SPA can deep-link from libx264 options
        # to ``#option-<name>``. Stays empty when --x264-repo wasn't
        # supplied or the commit's help text didn't parse.
        x264_help: dict[str, UpstreamOptionHelp] = {}
        x264_doc_path: str = ""
        if config.x264_repo is not None:
            if not released:
                logger.warn(
                    "x264 enrichment skipped: FFmpeg tag has no committer date"
                )
            else:
                commit = commit_at_or_before(config.x264_repo, released)
                if commit is None:
                    logger.warn(
                        f"x264 enrichment skipped: no x264 commit at or before "
                        f"{released}"
                    )
                else:
                    # Per-worker parse cache — when this worker has
                    # already handled another tag pinning the same x264
                    # commit, skip every file fetch and the parser run.
                    cached_doc = _x264_parse_cache.get(commit)
                    if cached_doc is not None:
                        x264_doc_struct = cached_doc  # type: ignore[assignment]
                        x264_c_text = "(cached)"
                        logger.debug(
                            f"x264 cache hit for commit {commit[:12]}"
                        )
                    else:
                        x264_c_text = show_file(
                            config.x264_repo, commit, "x264.c"
                        )

                    if x264_c_text is None:
                        logger.warn(
                            f"x264 enrichment skipped: x264.c not found at "
                            f"commit {commit[:12]}"
                        )
                    else:
                        if cached_doc is None:
                            # Optional auxiliary sources so the parser
                            # can resolve printf-style placeholders
                            # (``%d``, ``%.1f``, …) in the descriptions
                            # to actual constants and default values.
                            # Each is best-effort: missing → that
                            # resolution path is just skipped, the rest
                            # still works.
                            base_c = show_file(
                                config.x264_repo, commit, "common/base.c"
                            ) or ""
                            common_h = show_file(
                                config.x264_repo, commit, "common/common.h"
                            ) or ""
                            x264_h = show_file(
                                config.x264_repo, commit, "x264.h"
                            ) or ""
                            x264_doc_struct = parse_x264_doc(
                                x264_c_text,
                                base_c=base_c,
                                common_h=common_h,
                                x264_h=x264_h,
                            )
                            _x264_parse_cache[commit] = x264_doc_struct
                        x264_help = x264_doc_struct.options
                        if x264_help:
                            with_values = sum(1 for v in x264_help.values() if v.values)
                            with_desc = sum(1 for v in x264_help.values() if v.description)
                            logger.debug(
                                f"Parsed x264 help from commit {commit[:12]} "
                                f"(<= {released}): {len(x264_help)} options "
                                f"({with_values} with value lists, "
                                f"{with_desc} with descriptions)"
                            )
                            # Render the standalone HTML reference and
                            # record the per-version pointer that the
                            # SPA / index.json regenerator pick up.
                            x264_html = render_help_doc(
                                x264_doc_struct,
                                project="x264",
                                identifier=commit[:12],
                                identifier_kind="commit",
                                source_url=_THIRD_PARTY["x264"]["source_url"],
                                license_name=_THIRD_PARTY["x264"]["license"],
                                license_href=f"{_DOC_TO_ROOT}/licenses/{_THIRD_PARTY['x264']['license_file']}",
                                notices_href=f"{_DOC_TO_ROOT}/{_NOTICES_FILENAME}",
                                copyright_line=_THIRD_PARTY["x264"]["copyright"],
                            )
                            x264_doc_path = _emit_upstream_doc(
                                config.out, config.repo, "x264", commit[:12],
                                x264_html, logger,
                            )
                        else:
                            logger.warn(
                                f"x264 help text empty at commit {commit[:12]}; "
                                "libx264 -preset/-tune/-profile won't get values"
                            )

        # Upstream x265 help — same idea, but x265 publishes release tags,
        # so the snapshot is pinned to the most recent stable tag at or
        # before the FFmpeg release date. Parses the full CLI help
        # (x265cli.cpp's showHelp), resolving placeholders against
        # x265_param_default (param.cpp) + headers, and merges the
        # preset/tune/profile value lists from param.cpp/level.cpp.
        x265_help: dict[str, UpstreamOptionHelp] = {}
        x265_doc_path: str = ""
        if config.x265_repo is not None:
            if not released:
                logger.warn(
                    "x265 enrichment skipped: FFmpeg tag has no committer date"
                )
            else:
                x265_tag = tag_at_or_before(
                    config.x265_repo, released, _X265_STABLE_TAG
                )
                if x265_tag is None:
                    logger.warn(
                        f"x265 enrichment skipped: no x265 stable tag at or "
                        f"before {released}"
                    )
                else:
                    cached_x265 = _x265_parse_cache.get(x265_tag)
                    if cached_x265 is not None:
                        x265_doc_struct = cached_x265  # type: ignore[assignment]
                        logger.debug(f"x265 cache hit for tag {x265_tag}")
                    else:
                        def _x265_src(path: str) -> str:
                            return show_file(config.x265_repo, x265_tag, path) or ""

                        x265_doc_struct = parse_x265_doc(
                            _x265_src("source/x265cli.cpp"),
                            _x265_src("source/common/param.cpp"),
                            _x265_src("source/encoder/level.cpp"),
                            common_h=_x265_src("source/common/common.h"),
                            x265_h=_x265_src("source/x265.h"),
                        )
                        _x265_parse_cache[x265_tag] = x265_doc_struct

                    x265_help = x265_doc_struct.options
                    if x265_help:
                        with_desc = sum(
                            1 for v in x265_help.values() if v.description
                        )
                        logger.debug(
                            f"Parsed x265 help from tag {x265_tag} "
                            f"(<= {released}): {len(x265_help)} options "
                            f"({with_desc} with descriptions)"
                        )
                        x265_html = render_help_doc(
                            x265_doc_struct,
                            project="x265",
                            identifier=x265_tag,
                            identifier_kind="tag",
                            source_url=_THIRD_PARTY["x265"]["source_url"],
                            license_name=_THIRD_PARTY["x265"]["license"],
                            license_href=f"{_DOC_TO_ROOT}/licenses/{_THIRD_PARTY['x265']['license_file']}",
                            notices_href=f"{_DOC_TO_ROOT}/{_NOTICES_FILENAME}",
                            copyright_line=_THIRD_PARTY["x265"]["copyright"],
                            commercial_notice=_THIRD_PARTY["x265"]["commercial"],
                        )
                        x265_doc_path = _emit_upstream_doc(
                            config.out, config.repo, "x265", x265_tag,
                            x265_html, logger,
                        )
                    else:
                        logger.warn(
                            f"x265 help empty at tag {x265_tag}; "
                            "libx265 -preset/-tune/-profile won't get values"
                        )

        if "options" in config.categories:
            options = _extract_options(doc_root, makeinfo_cmd, logger, xml_cache)
            _write_json(output_dir / "options.json", {"options": options})

        if "codecs" in config.categories:
            codecs = _extract_codecs(
                doc_root, config.repo, tag, fallback_root, makeinfo_cmd, logger,
                c_map, xml_cache, x264_help, x265_help,
            )
            codec_options = _extract_codec_options(
                doc_root, makeinfo_cmd, logger, c_map, xml_cache,
            )
            _write_json(
                output_dir / "codecs.json",
                {"codec_options": codec_options, "codecs": codecs},
            )

        if "filters" in config.categories:
            filters = _extract_filters(doc_root, makeinfo_cmd, logger, xml_cache)
            _write_json(output_dir / "filters.json", {"filters": filters})

        if "demuxers" in config.categories:
            demuxers = _extract_named(
                doc_root, "demuxers.texi", parse_demuxers_xml, "demuxers",
                makeinfo_cmd, logger, xml_cache,
            )
            _attach_per_format_options(
                demuxers, doc_root, "demuxers.texi", "demuxer",
                makeinfo_cmd, logger, c_map, xml_cache,
            )
            _write_json(output_dir / "demuxers.json", {"demuxers": demuxers})

        if "muxers" in config.categories:
            muxers = _extract_named(
                doc_root, "muxers.texi", parse_muxers_xml, "muxers",
                makeinfo_cmd, logger, xml_cache,
            )
            _attach_per_format_options(
                muxers, doc_root, "muxers.texi", "muxer",
                makeinfo_cmd, logger, c_map, xml_cache,
            )
            format_options = _extract_format_options(
                doc_root, makeinfo_cmd, logger, c_map, xml_cache,
            )
            _write_json(
                output_dir / "muxers.json",
                {"format_options": format_options, "muxers": muxers},
            )

        if "protocols" in config.categories:
            protocols = _extract_named(
                doc_root, "protocols.texi", parse_protocols_xml, "protocols",
                makeinfo_cmd, logger, xml_cache,
            )
            _write_json(output_dir / "protocols.json", {"protocols": protocols})

        if "bitstream_filters" in config.categories:
            bsfs = _extract_named(
                doc_root, "bitstream_filters.texi", parse_bitstream_filters_xml,
                "bitstream_filters", makeinfo_cmd, logger, xml_cache,
            )
            _write_json(
                output_dir / "bitstream_filters.json",
                {"bitstream_filters": bsfs},
            )

        if config.html_doc:
            _generate_html_doc(
                doc_root, config.out, config.repo, target_version, tag,
                makeinfo_cmd, logger,
            )

    index = _build_index(
        target_version, released, config.categories,
        x264_doc=x264_doc_path, x265_doc=x265_doc_path,
    )
    _write_json(output_dir / "index.json", index)


def _generate_html_doc(
    doc_root: Path,
    out: Path,
    repo: Path,
    target_version: str,
    tag: str,
    makeinfo_cmd: list[str],
    logger: Logger,
) -> None:
    src = doc_root / "doc" / "ffmpeg.texi"
    if not src.exists():
        logger.warn(f"Skipping HTML doc for {target_version}: ffmpeg.texi missing")
        return

    # Stage the pinned-tag t2h.pm into the staged doc/, repointing its two CSS
    # hrefs at the shared copies one directory up (see _T2H_* constants). The
    # tag's own t2h.pm is not used: older tags target a removed Texinfo API.
    t2h_bytes = _pinned_asset_bytes(repo, _T2H_DOC_PATH)
    if t2h_bytes is None:
        logger.warn(
            f"Skipping HTML doc for {target_version}: {_PINNED_ASSET_TAG}:"
            f"{_T2H_DOC_PATH} not found in {repo} — is the {_PINNED_ASSET_TAG} "
            "tag present in --repo?"
        )
        return

    t2h_text = t2h_bytes.decode("utf-8")
    for old, new in _T2H_HREF_REPOINTS:
        if old not in t2h_text:
            logger.warn(
                f"Skipping HTML doc for {target_version}: expected CSS href "
                f"{old!r} not found in {_PINNED_ASSET_TAG}:{_T2H_DOC_PATH} — the "
                "pinned init file changed shape; update _T2H_HREF_REPOINTS."
            )
            return
        t2h_text = t2h_text.replace(old, new)

    staged_t2h = src.parent / "t2h.pm"
    staged_t2h.write_text(t2h_text, encoding="utf-8")

    doc_root_out = out / "doc" / "ffmpeg"
    if not _ensure_shared_assets(doc_root_out, repo, logger):
        logger.warn(
            f"Skipping HTML doc for {target_version}: shared CSS unavailable"
        )
        return

    output_path = doc_root_out / target_version / "ffmpeg-all.html"
    logger.info(f"Rendering HTML doc -> {output_path}")
    try:
        run_makeinfo_html(
            src,
            output_path,
            cwd=src.parent,
            cmd=makeinfo_cmd,
            init_file=staged_t2h,
        )
    except MakeinfoError as exc:
        logger.warn(f"HTML doc generation failed for {target_version}: {exc}")
        return

    _inject_ffmpeg_doc_footer(output_path, tag, logger)


def _inject_ffmpeg_doc_footer(output_path: Path, tag: str, logger: Logger) -> None:
    """Append the LGPL attribution footer to a rendered ``ffmpeg-all.html``.

    The page is produced by ``t2h.pm`` (makeinfo), not by our renderer, so the
    footer is spliced in by post-processing: inserted before the final
    ``</body>`` so it lands at the bottom of the document. A page missing
    ``</body>`` (unexpected) gets the footer appended and a warning.
    """
    info = _THIRD_PARTY["ffmpeg"]
    footer = build_generated_doc_footer(
        project_title=info["title"],
        snapshot_label=f"tag {tag}",
        source_url=info["source_url"],
        license_name=info["license"],
        license_href=f"{_DOC_TO_ROOT}/licenses/{info['license_file']}",
        notices_href=f"{_DOC_TO_ROOT}/{_NOTICES_FILENAME}",
        copyright_line=info["copyright"],
    )
    try:
        text = output_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warn(f"Could not read {output_path} to add license footer: {exc}")
        return

    marker = "</body>"
    idx = text.rfind(marker)
    if idx == -1:
        logger.warn(
            f"No </body> in {output_path}; appending license footer at end of file"
        )
        text = text + "\n" + footer
    else:
        text = text[:idx] + footer + text[idx:]
    output_path.write_text(text, encoding="utf-8")


def _ensure_shared_assets(doc_root_out: Path, repo: Path, logger: Logger) -> bool:
    """Fetch the shared CSS files from the pinned tag into ``doc_root_out``.

    Files already present (non-empty) are left alone. Returns ``True`` only if
    every shared CSS file is present afterward. Writes go through a
    PID-suffixed temp file + atomic replace so a concurrent worker never reads
    a half-written stylesheet.
    """
    doc_root_out.mkdir(parents=True, exist_ok=True)
    complete = True
    for name in _SHARED_CSS_FILES:
        dst = doc_root_out / name
        if dst.exists() and dst.stat().st_size > 0:
            continue
        data = _pinned_asset_bytes(repo, f"doc/{name}")
        if data is None:
            logger.warn(
                f"Shared asset doc/{name} not found at {_PINNED_ASSET_TAG} in "
                f"{repo}"
            )
            complete = False
            continue
        tmp = dst.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
    return complete


def _emit_upstream_doc(
    out_root: Path,
    repo: Path,
    project: str,
    identifier: str,
    html: str,
    logger: Logger,
) -> str:
    """Write a pre-rendered upstream reference page into
    ``<out_root>/doc/<project>/<identifier>/<project>-reference.html``
    and return its web-relative path for the per-version ``index.json``.

    Keyed on ``project:identifier`` (x264 commit SHA / x265 tag), so
    several FFmpeg tags pinning the same upstream snapshot share one
    file. Thread-safety is layered:

    1. **Per-worker memo** (:data:`_upstream_emit_cache`) — if this
       worker already emitted/confirmed this snapshot, return at once.
    2. **Cross-process lock** (:data:`_upstream_doc_lock`) — held across
       the write so concurrent workers don't both do the work. A
       ``nullcontext`` stands in for sequential mode.
    3. **File-exists fast path** inside the lock — the second worker
       reuses the first's output.
    4. **PID-suffixed tmp name** — defense in depth: no two workers
       compute the same ``.tmp`` name, so ``write_text`` can't collide.
       ``os.replace`` is atomic on POSIX and Windows.
    """
    key = f"{project}:{identifier}"
    relative = f"doc/{project}/{identifier}/{project}-reference.html"

    if key in _upstream_emit_cache:
        return relative

    page_dir = out_root / "doc" / project / identifier
    page_path = page_dir / f"{project}-reference.html"

    with _upstream_emit_context():
        if page_path.exists():
            _upstream_emit_cache.add(key)
            return relative

        # Ensure the shared CSS the page references exists (best-effort: an
        # unstyled page still beats no page).
        _ensure_shared_assets(out_root / "doc" / "ffmpeg", repo, logger)

        page_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = page_path.with_name(f"{page_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(html, encoding="utf-8")
        tmp_path.replace(page_path)
        _upstream_emit_cache.add(key)
        logger.debug(f"Rendered {project} reference -> {relative}")
    return relative


def _ensure_license_texts(config: ExtractConfig, logger: Logger) -> None:
    """Fetch each upstream's verbatim license text into ``<out>/licenses/``.

    FFmpeg's ``COPYING.LGPLv2.1`` comes from :data:`_PINNED_ASSET_TAG` (the
    text is invariant across the tag range, so one fetch covers every emitted
    version); x264 / x265 ship the GPL v2 text as ``COPYING`` and are fetched
    from ``HEAD`` of their respective clones (also invariant). Best-effort:
    a missing file is warned and skipped — the page footers/notices still
    reference it, but the deploy is then incomplete and the warning is loud.
    """
    out_dir = config.out / "licenses"
    out_dir.mkdir(parents=True, exist_ok=True)

    specs: list[tuple[Path, str, str, str]] = [
        (config.repo, _PINNED_ASSET_TAG, _FFMPEG_LICENSE_SRC, _THIRD_PARTY["ffmpeg"]["license_file"]),
    ]
    if config.x264_repo is not None:
        specs.append(
            (config.x264_repo, "HEAD", _UPSTREAM_LICENSE_SRC, _THIRD_PARTY["x264"]["license_file"])
        )
    if config.x265_repo is not None:
        specs.append(
            (config.x265_repo, "HEAD", _UPSTREAM_LICENSE_SRC, _THIRD_PARTY["x265"]["license_file"])
        )

    for repo, ref, src, name in specs:
        data = show_file_bytes(repo, ref, src)
        if data is None:
            logger.warn(
                f"License text {ref}:{src} not found in {repo}; "
                f"licenses/{name} not written (deploy attribution incomplete)"
            )
            continue
        dst = out_dir / name
        tmp = dst.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        logger.debug(f"Wrote third-party license text -> {dst}")


def _version_sort_key(name: str) -> tuple:
    """Sort ``major.minor`` version dir names numerically; fall back to the
    raw string for anything non-numeric so the sort never raises."""
    parts = name.split(".")
    if all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts))
    return (1, name)


def _generate_notices_page(config: ExtractConfig, logger: Logger) -> None:
    """Emit ``<out>/THIRD-PARTY-NOTICES.html`` listing every upstream the
    deploy redistributes derived artifacts from, the governing license, the
    bundled license text, and the exact snapshots that were emitted.

    Runs once in the parent after the per-tag loop. The set of snapshots is
    read back off the output tree (process-pool-safe — no need to thread data
    out of workers): FFmpeg versions from ``metadata/ffmpeg/<ver>/`` and the
    x264/x265 commits/tags from ``doc/<project>/<id>/``.
    """
    out = config.out

    def _subdir_names(base: Path) -> list[str]:
        if not base.is_dir():
            return []
        return [c.name for c in base.iterdir() if c.is_dir()]

    ffmpeg_versions = sorted(
        _subdir_names(out / "metadata" / "ffmpeg"), key=_version_sort_key
    )
    snapshots = {
        "ffmpeg": ffmpeg_versions,
        "x264": sorted(_subdir_names(out / "doc" / "x264")),
        "x265": sorted(_subdir_names(out / "doc" / "x265")),
    }
    license_dir = out / "licenses"

    sections: list[str] = []
    for slug, info in _THIRD_PARTY.items():
        ids = snapshots.get(slug, [])
        if slug != "ffmpeg" and not ids:
            # x264/x265 weren't part of this run — omit the section entirely.
            continue
        license_file = info["license_file"]
        has_text = (license_dir / license_file).is_file()
        license_link = (
            f'<a href="licenses/{html.escape(license_file)}">{html.escape(info["license"])}</a>'
            if has_text
            else html.escape(info["license"])
        )
        parts = [
            f'  <section class="tp">',
            f'    <h2>{html.escape(info["title"])}</h2>',
            f'    <p>{html.escape(info["copyright"])}</p>',
            f'    <p>Licensed under {license_link}.',
        ]
        if info.get("commercial"):
            parts.append(f'    {html.escape(info["commercial"])}')
        parts.append("    </p>")
        parts.append(
            f'    <p>The bundles and reference pages here are generated from '
            f'{html.escape(info["derived_from"])}; they are not the original source. '
            f'Corresponding source: '
            f'<a href="{html.escape(info["source_url"])}" rel="noreferrer">'
            f'{html.escape(info["source_url"])}</a>.</p>'
        )
        if ids:
            label = "Versions" if slug == "ffmpeg" else (
                "Commits" if slug == "x264" else "Tags"
            )
            shown = ", ".join(html.escape(i) for i in ids)
            parts.append(
                f'    <p class="tp-snaps">{label} included: {shown}</p>'
            )
        parts.append("  </section>")
        sections.append("\n".join(parts))

    page = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Third-party licenses &amp; attribution</title>\n"
        "  <style>\n"
        "    body { font-family: system-ui, sans-serif; max-width: 820px; margin: 0 auto;\n"
        "           padding: 2rem 1.5rem; color: #24292e; line-height: 1.55; }\n"
        "    h1 { font-size: 1.6rem; } h2 { font-size: 1.2rem; margin-top: 0; }\n"
        "    .tp { border: 1px solid #e1e4e8; border-radius: 6px; padding: 1rem 1.25rem;\n"
        "          margin-bottom: 1.25rem; }\n"
        "    .tp-snaps { color: #666; font-size: 0.85rem; }\n"
        "    a { color: #0366d6; }\n"
        "    .intro { color: #444; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Third-party licenses &amp; attribution</h1>\n"
        '  <p class="intro">Simply FFmpeg Parser is MIT-licensed. The per-version metadata\n'
        "  bundles and the rendered HTML reference pages it serves are <strong>derivative\n"
        "  works</strong> generated from the upstream projects below and are distributed\n"
        "  under each project's own license, not MIT. The shared <code>bootstrap.min.css</code>\n"
        "  and <code>style.min.css</code> are MIT and carry their own notices.</p>\n"
        + "\n".join(sections)
        + "\n</body>\n</html>\n"
    )

    dst = out / _NOTICES_FILENAME
    dst.write_text(page, encoding="utf-8")
    logger.info(f"Wrote third-party notices -> {dst}")


def run_extraction(config: ExtractConfig) -> int:
    logger = Logger(config.verbose)

    if not config.repo.exists():
        logger.warn("Repository path does not exist")
        return 1

    if not (config.repo / ".git").exists():
        logger.warn("Repository path does not look like a Git repo")
        return 1

    try:
        makeinfo_cmd = resolve_makeinfo()
    except MakeinfoError as exc:
        logger.warn(str(exc))
        return 1
    logger.debug(f"Using makeinfo: {' '.join(makeinfo_cmd)}")

    try:
        tags = select_tags(config, logger)
    except ExtractionError as exc:
        logger.warn(str(exc))
        return 2

    failures: list[str] = []
    worker_count = max(1, min(config.jobs, len(tags)))

    if worker_count <= 1:
        for tag in tags:
            tag_logger = Logger(config.verbose, tag=tag)
            try:
                _extract_for_tag(config, tag, makeinfo_cmd, tag_logger)
            except ExtractionError as exc:
                tag_logger.warn(f"Extraction failed: {exc}")
                failures.append(tag)
                if not config.continue_on_error:
                    return 3
    else:
        logger.debug(f"Extracting {len(tags)} tags with {worker_count} workers")
        # ProcessPool, not ThreadPool: per-tag work is dominated by external
        # ``makeinfo`` invocations (already separate processes) plus Python
        # parsing of the resulting XML and the libav* C source — the latter
        # is CPU-bound and would contend on the GIL with threads. Each
        # worker gets its own tempdir per tag, so file-system isolation is
        # already in place.
        #
        # Workers ship their log lines to the parent over ``log_queue``; a
        # parent-side drain thread is the sole console writer (streaming, no
        # buffering). A shared lock alone wouldn't fix the multi-process
        # console tearing on Windows (the ``♪◙`` regression) — only one
        # process writing does.
        #
        # We don't pre-flight ``--continue-on-error: False``: with multiple
        # tags already in flight, "bail at first error" can't unwind the
        # in-flight work. Instead, we let all submitted futures finish and
        # then return non-zero — same exit code as before, slightly more
        # work done than necessary in the failure case.
        mp_ctx = multiprocessing.get_context()
        log_queue = mp_ctx.Queue()
        upstream_doc_lock = mp_ctx.Lock()
        drain_thread = threading.Thread(
            target=_drain_log_queue, args=(log_queue,), daemon=True
        )
        drain_thread.start()
        try:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                mp_context=mp_ctx,
                initializer=_pool_initializer,
                initargs=(log_queue, upstream_doc_lock),
            ) as pool:
                future_to_tag = {
                    pool.submit(_extract_for_tag_pooled, config, tag, makeinfo_cmd): tag
                    for tag in tags
                }
                for future in concurrent.futures.as_completed(future_to_tag):
                    tag = future_to_tag[future]
                    try:
                        ok = future.result()
                    except Exception as exc:
                        # The worker function itself failed to *return* —
                        # segfault, pickle error, OOM. No log surfaces; report
                        # the crash and move on. ``warn`` carries the
                        # ``[{tag}]`` prefix like the rest.
                        logger.warn(
                            f"[{tag}] Worker died: {type(exc).__name__}: {exc}"
                        )
                        failures.append(tag)
                        continue
                    if not ok:
                        failures.append(tag)
        finally:
            # Pool is shut down (workers exited gracefully and flushed their
            # queued lines); stop the drain thread once it has written them.
            log_queue.put(None)
            drain_thread.join()

    # Third-party license texts + aggregate notices page. Emitted once in the
    # parent regardless of per-tag failures (any artifact that did land must
    # still carry its attribution); the notices page reflects whatever
    # snapshots actually made it to the output tree.
    _ensure_license_texts(config, logger)
    _generate_notices_page(config, logger)

    if failures:
        logger.warn(f"Extraction failed for tags: {', '.join(failures)}")
        return 3

    return 0
