"""Thin compatibility shim — the HTML renderer now lives in
:mod:`.upstream_help` (shared by x264 and x265). This module keeps the
``render_x264_doc`` entry point so existing imports/tests don't break.
"""

from __future__ import annotations

from .upstream_help import HelpDoc, render_help_doc

_X264_SOURCE_URL = "https://code.videolan.org/videolan/x264"


def render_x264_doc(
    doc: HelpDoc,
    *,
    x264_commit: str = "",
    x264_tag: str = "",
    source_url: str = _X264_SOURCE_URL,
) -> str:
    """Render the x264 reference page. x264 publishes no release tags,
    so the identifier is normally a commit SHA."""
    identifier = x264_tag or x264_commit
    identifier_kind = "tag" if x264_tag else "commit"
    return render_help_doc(
        doc,
        project="x264",
        identifier=identifier,
        identifier_kind=identifier_kind,
        source_url=source_url,
    )
