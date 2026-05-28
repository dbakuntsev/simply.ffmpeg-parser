"""HTML renderer for the section-aware x264 help text.

Consumes the :class:`.x264_help.X264HelpDoc` produced by
:func:`.x264_help.parse_x264_doc` and emits one self-contained HTML
file styled to match the existing ``ffmpeg-all.html`` documentation
(shared Bootstrap + ``style.min.css`` via cross-folder reference, so
no asset duplication and no extra deploy step).

The output layout is:

- A page heading with the x264 commit identifier and date the page
  was rendered against.
- A sidebar table-of-contents with one link per section.
- One ``<section>`` per section in source order, each containing:

  - An anchored ``<h2 id="section-...">`` title.
  - A definition list (``<dl>``) of options. Each option has:

    - ``<dt id="option-<name>">`` carrying the option name and (when
      present) the documented ``<type>`` argument.
    - ``<dd>`` with the resolved header description followed, for
      enum-typed options, by a nested ``<dl>`` of value-name → value
      description.

Stable ``id`` attributes mean the SPA's inspector can deep-link from
a libx264 option directly to its section (``#option-crf``,
``#option-preset``, etc.) without any further coordination.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from .x264_help import X264HelpDoc, X264Section, UpstreamOptionHelp


# Stylesheet path is relative to the rendered page's directory.
# Page lives at ``<out>/doc/x264/<commit12>/x264-reference.html``, so two
# ``..`` segments climb to ``<out>/doc/`` and then enter ``ffmpeg/`` for
# the shared assets already vendored there for the FFmpeg HTML reference.
_SHARED_CSS_PREFIX = "../../ffmpeg"


_NON_ANCHOR_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def _anchor_id(prefix: str, name: str) -> str:
    """Build a URL-fragment-safe id like ``option-crf`` or
    ``section-frame-type-options``. Non-alphanumeric characters are
    collapsed to a single hyphen and surrounding hyphens are stripped."""
    slug = _NON_ANCHOR_CHARS.sub("-", name.strip().lower()).strip("-")
    return f"{prefix}-{slug}" if slug else prefix


def _format_description(text: str) -> str:
    """Render a parsed option description to HTML.

    Newlines in the source (intentional wraps in the x264 help output)
    become ``<br>`` so the page reads the same as ``--fullhelp``. The
    content itself is HTML-escaped so any ``<``/``>`` in default values
    (e.g. ``["<unset>"]`` placeholders) doesn't break the markup.
    """
    if not text:
        return ""
    escaped = html.escape(text)
    return escaped.replace("\n", "<br>")


def _render_value_table(values: list[tuple[str, str]]) -> str:
    """Render a ``--preset`` / ``--tune`` / ``--profile`` value list as
    a definition list. Each ``<dt>`` is the value name in monospace;
    each ``<dd>`` is its description (multi-line, preserving newlines
    via ``white-space: pre-line`` inline)."""
    rows: list[str] = []
    for name, desc in values:
        rows.append(
            f'        <dt class="x264-value"><code>{html.escape(name)}</code></dt>'
        )
        if desc:
            rendered = _format_description(desc)
            rows.append(
                f'        <dd class="x264-value-desc">{rendered}</dd>'
            )
        else:
            rows.append(
                '        <dd class="x264-value-desc x264-value-empty">—</dd>'
            )
    return (
        '      <dl class="x264-values">\n'
        + "\n".join(rows)
        + "\n      </dl>"
    )


def _render_option(name: str, info: UpstreamOptionHelp) -> str:
    """One ``<dt>``+``<dd>`` pair for a single option entry."""
    anchor = _anchor_id("option", name)
    desc_html = _format_description(info.description) or "<em>No description.</em>"
    parts = [
        f'    <dt id="{html.escape(anchor)}" class="x264-option">'
        f'<code>--{html.escape(name)}</code>'
        f' <a class="x264-anchor" href="#{html.escape(anchor)}" '
        f'title="Permalink to this option">¶</a></dt>',
        f'    <dd class="x264-option-desc">{desc_html}',
    ]
    if info.values:
        parts.append(_render_value_table(info.values))
    parts.append("    </dd>")
    return "\n".join(parts)


def _render_section(section: X264Section) -> str:
    """One ``<section>`` block with the section's options as a ``<dl>``."""
    anchor = _anchor_id("section", section.title)
    items = [_render_option(name, info) for name, info in section.options]
    return (
        f'  <section class="x264-section" id="{html.escape(anchor)}">\n'
        f'    <h2><a href="#{html.escape(anchor)}" '
        f'class="x264-anchor">{html.escape(section.title)}</a></h2>\n'
        f'    <dl class="x264-options">\n'
        + "\n".join(items)
        + "\n    </dl>\n  </section>"
    )


def _render_toc(sections: list[X264Section]) -> str:
    """Sidebar TOC. Each entry links to its section anchor."""
    rows: list[str] = []
    for s in sections:
        anchor = _anchor_id("section", s.title)
        rows.append(
            f'      <li><a href="#{html.escape(anchor)}">'
            f'{html.escape(s.title)}</a> '
            f'<span class="x264-count">({len(s.options)})</span></li>'
        )
    return (
        '    <ul class="x264-toc-list">\n'
        + "\n".join(rows)
        + "\n    </ul>"
    )


_PAGE_STYLE = """
    body { background: #fff; }
    .x264-doc { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
    .x264-doc h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
    .x264-doc .x264-meta { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
    .x264-layout { display: grid; grid-template-columns: 16rem 1fr; gap: 2rem; align-items: start; }
    @media (max-width: 800px) {
      .x264-layout { grid-template-columns: 1fr; }
      .x264-toc { position: static !important; }
    }
    .x264-toc { position: sticky; top: 1rem; max-height: calc(100vh - 2rem); overflow-y: auto;
                border: 1px solid #ddd; border-radius: 4px; padding: 0.75rem 1rem; background: #f8f8f8; }
    .x264-toc h2 { font-size: 0.9rem; margin: 0 0 0.5rem 0; color: #333; text-transform: uppercase; letter-spacing: 0.05em; }
    .x264-toc-list { list-style: none; padding: 0; margin: 0; font-size: 0.9rem; line-height: 1.5; }
    .x264-toc-list a { color: #0366d6; text-decoration: none; }
    .x264-toc-list a:hover { text-decoration: underline; }
    .x264-toc-list .x264-count { color: #999; font-size: 0.8rem; }
    .x264-section { margin-bottom: 2rem; }
    .x264-section h2 { font-size: 1.25rem; padding-bottom: 0.25rem; border-bottom: 1px solid #ddd; }
    .x264-section h2 .x264-anchor { color: inherit; text-decoration: none; }
    .x264-options { margin: 0; }
    .x264-option { margin-top: 1rem; }
    .x264-option code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 3px;
                       font-size: 0.95em; color: #24292e; }
    .x264-anchor { color: #ccc; text-decoration: none; font-weight: normal; margin-left: 0.25rem;
                   opacity: 0; transition: opacity 0.15s; }
    .x264-option:hover .x264-anchor,
    .x264-section h2:hover .x264-anchor { opacity: 1; }
    .x264-option-desc { margin-left: 1.25rem; margin-bottom: 0.5rem; color: #333; }
    .x264-values { margin: 0.5rem 0 0.5rem 1rem; display: grid;
                   grid-template-columns: max-content 1fr; gap: 0.15rem 1rem;
                   border-left: 3px solid #eee; padding: 0.25rem 0 0.25rem 0.75rem; }
    .x264-values dt, .x264-values dd { margin: 0; }
    .x264-value-desc { white-space: pre-line; color: #444; font-size: 0.9em; }
    .x264-value-empty { color: #bbb; }
    :target { background-color: #fff8c5; transition: background-color 1s ease-out; }
"""


def render_x264_doc(
    doc: X264HelpDoc,
    *,
    x264_commit: str = "",
    x264_tag: str = "",
    source_url: str = "https://code.videolan.org/videolan/x264",
) -> str:
    """Return one self-contained HTML page for ``doc``.

    ``x264_commit`` and ``x264_tag`` are surfaced in the page header so a
    reader can tell exactly which snapshot of x264 they're looking at.
    Both are optional; either or both may be empty.

    The page references the shared Bootstrap + style CSS that the
    extractor already vendors for ffmpeg-all.html, via a relative path
    that assumes this page lives at ``doc/x264/<id>/x264-reference.html``
    and the shared assets live at ``doc/ffmpeg/*.css``.
    """
    title = "x264 reference"
    if x264_tag:
        title = f"{title} ({x264_tag})"
    elif x264_commit:
        title = f"{title} ({x264_commit[:12]})"

    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    meta_parts = []
    if x264_commit:
        meta_parts.append(
            f'Commit <code>{html.escape(x264_commit[:12])}</code>'
        )
    if x264_tag:
        meta_parts.append(f'Tag <code>{html.escape(x264_tag)}</code>')
    meta_parts.append(
        f'Rendered <time>{html.escape(rendered_at)}</time>'
    )
    meta_parts.append(
        f'Source: <a href="{html.escape(source_url)}" '
        f'rel="noreferrer">{html.escape(source_url)}</a>'
    )
    meta = " · ".join(meta_parts)

    sections_html = "\n".join(_render_section(s) for s in doc.sections)

    return (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'  <title>{html.escape(title)}</title>\n'
        f'  <link rel="stylesheet" href="{_SHARED_CSS_PREFIX}/bootstrap.min.css">\n'
        f'  <link rel="stylesheet" href="{_SHARED_CSS_PREFIX}/style.min.css">\n'
        '  <style>'
        + _PAGE_STYLE
        + '  </style>\n'
        '</head>\n'
        '<body>\n'
        '<main class="x264-doc">\n'
        f'  <h1>{html.escape(title)}</h1>\n'
        f'  <div class="x264-meta">{meta}</div>\n'
        '  <div class="x264-layout">\n'
        '    <nav class="x264-toc" aria-label="Sections">\n'
        '      <h2>Contents</h2>\n'
        + _render_toc(doc.sections)
        + '\n    </nav>\n'
        '    <div class="x264-body">\n'
        + sections_html
        + '\n    </div>\n'
        '  </div>\n'
        '</main>\n'
        '</body>\n'
        '</html>\n'
    )
