"""Third-party attribution: license-text fetch, upstream reference-page
emission, and the aggregate ``THIRD-PARTY-NOTICES.html``.

The distributed artifacts (per-version JSON, the FFmpeg ``ffmpeg-all.html``
reference, and the x264/x265 reference pages) are derivative works of GPL /
LGPL upstreams, so the deploy must carry each upstream's license text, a
copyright notice, and a pointer to the corresponding source. We fetch the
verbatim ``COPYING`` file from each repo at build time into ``<out>/licenses``
(never vendored — keeps this repo's tree 100% MIT) and emit one aggregate
``THIRD-PARTY-NOTICES.html`` at the output root.

FFmpeg is consumed only via its documentation + ``libav*`` headers (none of
the GPL-only files), so the LGPL v2.1 text is the governing license; x264 and
x265 both ship the GPL v2 text as ``COPYING`` and are "v2 or later".
"""

from __future__ import annotations

import contextlib
import html
import json
import os
from pathlib import Path

from ._logging import Logger
from .git_utils import show_file_bytes
from .models import ExtractConfig
from .pinned_assets import PINNED_ASSET_TAG, ensure_shared_assets

_FFMPEG_LICENSE_SRC = "COPYING.LGPLv2.1"
_UPSTREAM_LICENSE_SRC = "COPYING"

# Relative prefix from a rendered doc page (always 3 levels deep:
# ``doc/ffmpeg/<ver>/``, ``doc/x264/<id>/``, ``doc/x265/<id>/``) back to the
# output root, where ``licenses/`` and ``THIRD-PARTY-NOTICES.html`` live.
DOC_TO_ROOT = "../../.."
NOTICES_FILENAME = "THIRD-PARTY-NOTICES.html"

# Static descriptor for each upstream, keyed by the slug used in output paths.
# ``license_file`` is the name written under ``<out>/licenses/``.
THIRD_PARTY = {
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

# Cross-process lock that serializes upstream-library reference HTML
# writes (x264 + x265). Several FFmpeg tags often pin the same upstream
# snapshot (e.g. n8.1 and n8.1.1 both → x264 0480cb05fa18) and without
# this lock concurrent workers would race to write the same
# ``doc/<project>/<id>/<project>-reference.html``. Held only during the
# render+write inside :func:`emit_upstream_doc`, with a check-existing
# fast path so the second worker reuses the first's output for free.
_upstream_doc_lock = None

# Per-worker memo of ``project:identifier`` snapshots this worker has
# already emitted (or confirmed on disk). Lets the second tag pinning the
# same snapshot skip even the lock acquisition + file-stat round-trip.
_upstream_emit_cache: set[str] = set()


def set_upstream_doc_lock(lock) -> None:
    """Install the shared cross-process lock in this worker process. Called
    by the pool initializer; sequential runs leave the lock ``None`` (a
    nullcontext stands in)."""
    global _upstream_doc_lock
    _upstream_doc_lock = lock


def _upstream_emit_context():
    """Context manager wrapping the upstream-doc emit phase with the
    shared cross-process lock when installed (parallel mode), or a no-op
    otherwise (sequential mode — no race possible)."""
    return (
        _upstream_doc_lock
        if _upstream_doc_lock is not None
        else contextlib.nullcontext()
    )


def emit_upstream_doc(
    out_root: Path,
    repo: Path,
    project: str,
    identifier: str,
    html_content: str,
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
        ensure_shared_assets(out_root / "doc" / "ffmpeg", repo, logger)

        page_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = page_path.with_name(f"{page_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(html_content, encoding="utf-8")
        tmp_path.replace(page_path)
        _upstream_emit_cache.add(key)
        logger.debug(f"Rendered {project} reference -> {relative}")
    return relative


def ensure_license_texts(config: ExtractConfig, logger: Logger) -> None:
    """Fetch each upstream's verbatim license text into ``<out>/licenses/``.

    FFmpeg's ``COPYING.LGPLv2.1`` comes from :data:`PINNED_ASSET_TAG` (the
    text is invariant across the tag range, so one fetch covers every emitted
    version); x264 / x265 ship the GPL v2 text as ``COPYING`` and are fetched
    from ``HEAD`` of their respective clones (also invariant). Best-effort:
    a missing file is warned and skipped — the page footers/notices still
    reference it, but the deploy is then incomplete and the warning is loud.
    """
    out_dir = config.out / "licenses"
    out_dir.mkdir(parents=True, exist_ok=True)

    specs: list[tuple[Path, str, str, str]] = [
        (config.repo, PINNED_ASSET_TAG, _FFMPEG_LICENSE_SRC, THIRD_PARTY["ffmpeg"]["license_file"]),
    ]
    if config.x264_repo is not None:
        specs.append(
            (config.x264_repo, "HEAD", _UPSTREAM_LICENSE_SRC, THIRD_PARTY["x264"]["license_file"])
        )
    if config.x265_repo is not None:
        specs.append(
            (config.x265_repo, "HEAD", _UPSTREAM_LICENSE_SRC, THIRD_PARTY["x265"]["license_file"])
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


def generate_notices_page(config: ExtractConfig, logger: Logger) -> None:
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

    # Cite the exact patch-level tag (e.g. ``n8.1.1``), not the rolled-up
    # ``major.minor`` directory name — read it back from each version's
    # index.json (written with a ``tag`` field by _build_index). Fall back to
    # the directory name when the tag is absent (older bundle on disk).
    ffmpeg_meta = out / "metadata" / "ffmpeg"
    ffmpeg_tags: list[str] = []
    for ver in sorted(_subdir_names(ffmpeg_meta), key=_version_sort_key):
        tag = ""
        index_file = ffmpeg_meta / ver / "index.json"
        if index_file.is_file():
            try:
                tag = json.loads(index_file.read_text(encoding="utf-8")).get("tag", "")
            except (OSError, ValueError):
                tag = ""
        ffmpeg_tags.append(tag or ver)
    snapshots = {
        "ffmpeg": ffmpeg_tags,
        "x264": sorted(_subdir_names(out / "doc" / "x264")),
        "x265": sorted(_subdir_names(out / "doc" / "x265")),
    }
    license_dir = out / "licenses"

    sections: list[str] = []
    for slug, info in THIRD_PARTY.items():
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
            label = "Commits" if slug == "x264" else "Tags"
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

    dst = out / NOTICES_FILENAME
    dst.write_text(page, encoding="utf-8")
    logger.info(f"Wrote third-party notices -> {dst}")
