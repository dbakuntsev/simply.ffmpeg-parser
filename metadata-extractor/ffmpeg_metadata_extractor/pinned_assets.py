"""Build-time fetch of pinned-tag FFmpeg assets.

HTML rendering assets (``t2h.pm`` + the two CSS files) are fetched from a
pinned FFmpeg tag at build time rather than vendored into the package, so
no GPL-licensed bytes (``t2h.pm`` is "part of FFmpeg", GPLv3+) are
committed to this MIT repo — consistent with the project rule that
GPL-derived artifacts are generated at build time and never checked in.

The tag is pinned (not "the tag being rendered") because n8.1.1's
``t2h.pm`` is version-gated for Texinfo 7.1+, while older tags' copies
call APIs (e.g. ``$self->gdt``) that 7.1 removed; the n8.1.1 init file is
the one that renders across the whole tag range. The same pinned tag
supplies the shared ``bootstrap.min.css`` / ``style.min.css`` used by
both the FFmpeg ``ffmpeg-all.html`` reference and the x264/x265 upstream
reference pages.
"""

from __future__ import annotations

import os
from pathlib import Path

from .git_utils import show_file_bytes

PINNED_ASSET_TAG = "n8.1.1"
SHARED_CSS_FILES = ("bootstrap.min.css", "style.min.css")

# Per-process cache of pinned-tag asset bytes keyed by repo-relative path
# (``None`` = confirmed absent). Pool workers are separate processes, so each
# shells out to ``git show`` at most once per asset.
_pinned_asset_cache: dict[str, bytes | None] = {}


def pinned_asset_bytes(repo: Path, path: str) -> bytes | None:
    """Return ``path`` at :data:`PINNED_ASSET_TAG` from ``repo``, memoized."""
    if path not in _pinned_asset_cache:
        _pinned_asset_cache[path] = show_file_bytes(repo, PINNED_ASSET_TAG, path)
    return _pinned_asset_cache[path]


def ensure_shared_assets(doc_root_out: Path, repo: Path, logger) -> bool:
    """Fetch the shared CSS files from the pinned tag into ``doc_root_out``.

    Files already present (non-empty) are left alone. Returns ``True`` only if
    every shared CSS file is present afterward. Writes go through a
    PID-suffixed temp file + atomic replace so a concurrent worker never reads
    a half-written stylesheet.
    """
    doc_root_out.mkdir(parents=True, exist_ok=True)
    complete = True
    for name in SHARED_CSS_FILES:
        dst = doc_root_out / name
        if dst.exists() and dst.stat().st_size > 0:
            continue
        data = pinned_asset_bytes(repo, f"doc/{name}")
        if data is None:
            logger.warn(
                f"Shared asset doc/{name} not found at {PINNED_ASSET_TAG} in "
                f"{repo}"
            )
            complete = False
            continue
        tmp = dst.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
    return complete
