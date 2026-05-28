"""Shared data structures and HTML renderer for upstream-library help
documentation (x264, x265, and any future codec wrapper).

The per-library parsers (:mod:`.x264_help`, :mod:`.x265_help`) produce a
:class:`HelpDoc` — an ordered list of :class:`HelpSection`, each holding
``(option_name, UpstreamOptionHelp)`` pairs — plus a flat by-name index
the extractor uses to overlay descriptions/value-lists onto the matching
FFmpeg codec options.

:func:`render_help_doc` turns a :class:`HelpDoc` into one self-contained
HTML page styled to match the existing ``ffmpeg-all.html`` reference
(shared Bootstrap + ``style.min.css`` via a cross-folder reference, so
no asset duplication). Stable ``#option-<name>`` / ``#section-<slug>``
anchors let the SPA inspector deep-link straight to an option.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class UpstreamOptionHelp:
    """Per-option help text mined from an upstream library's source.

    ``description``: the option header line (one short paragraph,
    e.g. ``Quality-based VBR (0-51) [23.0]`` from x264's ``--crf``).
    Empty when the source documents the option only as a value list, or
    when the option couldn't be found.

    ``values``: list of ``(value_name, value_description)`` pairs for
    enum-style options (x264/x265 preset, tune, profile). Empty for
    options whose value is a free-form number/string.

    The two fields are independent: an option may have only a
    description, only values, both, or neither.
    """

    description: str = ""
    values: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class HelpSection:
    """A heading-grouped run of options from a library's ``--fullhelp``.

    ``title`` is the section header without its trailing colon
    (``"Ratecontrol"``, ``"Profile, Level, Tier"``). ``options`` lists
    ``(option_name, help)`` pairs in source order.
    """

    title: str
    options: list[tuple[str, UpstreamOptionHelp]] = field(default_factory=list)


@dataclass(frozen=True)
class HelpDoc:
    """Section-ordered view of a library's help text plus a by-name index.

    ``sections`` is what the doc renderer iterates; ``options`` is the
    flat lookup the extractor's option-overlay step uses. Both views
    reference the same :class:`UpstreamOptionHelp` instances.
    """

    sections: list[HelpSection] = field(default_factory=list)
    options: dict[str, UpstreamOptionHelp] = field(default_factory=dict)


# --- HTML rendering --------------------------------------------------------

# Page lives at ``<out>/doc/<project>/<id>/<project>-reference.html``, so
# two ``..`` segments climb to ``<out>/doc/`` and then enter ``ffmpeg/``
# for the shared CSS the FFmpeg HTML reference also uses.
_SHARED_CSS_PREFIX = "../../ffmpeg"

_NON_ANCHOR_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def _anchor_id(prefix: str, name: str) -> str:
    """Build a URL-fragment-safe id like ``option-crf`` or
    ``section-frame-type-options``."""
    slug = _NON_ANCHOR_CHARS.sub("-", name.strip().lower()).strip("-")
    return f"{prefix}-{slug}" if slug else prefix


def _format_description(text: str) -> str:
    """Render a parsed description to HTML. Newlines (intentional wraps
    in the help output) become ``<br>``; content is HTML-escaped."""
    if not text:
        return ""
    return html.escape(text).replace("\n", "<br>")


def _render_value_table(values: list[tuple[str, str]]) -> str:
    rows: list[str] = []
    for name, desc in values:
        rows.append(
            f'        <dt class="x-value"><code>{html.escape(name)}</code></dt>'
        )
        if desc:
            rows.append(f'        <dd class="x-value-desc">{_format_description(desc)}</dd>')
        else:
            rows.append('        <dd class="x-value-desc x-value-empty">—</dd>')
    return '      <dl class="x-values">\n' + "\n".join(rows) + "\n      </dl>"


def _render_option(name: str, info: UpstreamOptionHelp) -> str:
    anchor = _anchor_id("option", name)
    desc_html = _format_description(info.description) or "<em>No description.</em>"
    parts = [
        f'    <dt id="{html.escape(anchor)}" class="x-option">'
        f'<code>--{html.escape(name)}</code>'
        f' <a class="x-anchor" href="#{html.escape(anchor)}" '
        f'title="Permalink to this option">¶</a></dt>',
        f'    <dd class="x-option-desc">{desc_html}',
    ]
    if info.values:
        parts.append(_render_value_table(info.values))
    parts.append("    </dd>")
    return "\n".join(parts)


def _render_section(section: HelpSection) -> str:
    anchor = _anchor_id("section", section.title)
    items = [_render_option(name, info) for name, info in section.options]
    return (
        f'  <section class="x-section" id="{html.escape(anchor)}">\n'
        f'    <h2><a href="#{html.escape(anchor)}" '
        f'class="x-anchor">{html.escape(section.title)}</a></h2>\n'
        f'    <dl class="x-options">\n'
        + "\n".join(items)
        + "\n    </dl>\n  </section>"
    )


def _render_toc(sections: list[HelpSection]) -> str:
    rows: list[str] = []
    for s in sections:
        anchor = _anchor_id("section", s.title)
        rows.append(
            f'      <li><a href="#{html.escape(anchor)}">'
            f'{html.escape(s.title)}</a> '
            f'<span class="x-count">({len(s.options)})</span></li>'
        )
    return '    <ul class="x-toc-list">\n' + "\n".join(rows) + "\n    </ul>"


_PAGE_STYLE = """
    body { background: #fff; }
    .x-doc { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
    .x-doc h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
    .x-doc .x-meta { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
    .x-layout { display: grid; grid-template-columns: 16rem 1fr; gap: 2rem; align-items: start; }
    @media (max-width: 800px) {
      .x-layout { grid-template-columns: 1fr; }
      .x-toc { position: static !important; }
    }
    .x-toc { position: sticky; top: 1rem; max-height: calc(100vh - 2rem); overflow-y: auto;
             border: 1px solid #ddd; border-radius: 4px; padding: 0.75rem 1rem; background: #f8f8f8; }
    .x-toc h2 { font-size: 0.9rem; margin: 0 0 0.5rem 0; color: #333; text-transform: uppercase; letter-spacing: 0.05em; }
    .x-toc-list { list-style: none; padding: 0; margin: 0; font-size: 0.9rem; line-height: 1.5; }
    .x-toc-list a { color: #0366d6; text-decoration: none; }
    .x-toc-list a:hover { text-decoration: underline; }
    .x-toc-list .x-count { color: #999; font-size: 0.8rem; }
    .x-section { margin-bottom: 2rem; }
    .x-section h2 { font-size: 1.25rem; padding-bottom: 0.25rem; border-bottom: 1px solid #ddd; }
    .x-section h2 .x-anchor { color: inherit; text-decoration: none; }
    .x-options { margin: 0; }
    .x-option { margin-top: 1rem; }
    .x-option code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 3px;
                     font-size: 0.95em; color: #24292e; }
    .x-anchor { color: #ccc; text-decoration: none; font-weight: normal; margin-left: 0.25rem;
                opacity: 0; transition: opacity 0.15s; }
    .x-option:hover .x-anchor,
    .x-section h2:hover .x-anchor { opacity: 1; }
    .x-option-desc { margin-left: 1.25rem; margin-bottom: 0.5rem; color: #333; }
    .x-values { margin: 0.5rem 0 0.5rem 1rem; display: grid;
                grid-template-columns: max-content 1fr; gap: 0.15rem 1rem;
                border-left: 3px solid #eee; padding: 0.25rem 0 0.25rem 0.75rem; }
    .x-values dt, .x-values dd { margin: 0; }
    .x-value-desc { white-space: pre-line; color: #444; font-size: 0.9em; }
    .x-value-empty { color: #bbb; }
    :target { background-color: #fff8c5; transition: background-color 1s ease-out; }
"""


def render_help_doc(
    doc: HelpDoc,
    *,
    project: str,
    identifier: str = "",
    identifier_kind: str = "commit",
    source_url: str = "",
) -> str:
    """Return one self-contained HTML page for ``doc``.

    ``project`` is the upstream library name (``"x264"`` / ``"x265"``)
    used in the page title and anchors-by-convention. ``identifier`` is
    the snapshot identity (a commit SHA or release tag); ``identifier_kind``
    labels it (``"commit"`` / ``"tag"``). ``source_url`` points at the
    upstream repository. All but ``project`` are optional.
    """
    short_id = identifier
    if identifier_kind == "commit" and identifier:
        short_id = identifier[:12]

    title = f"{project} reference"
    if short_id:
        title = f"{title} ({short_id})"

    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    meta_parts: list[str] = []
    if identifier:
        label = identifier_kind.capitalize()
        meta_parts.append(f"{label} <code>{html.escape(short_id)}</code>")
    meta_parts.append(f"Rendered <time>{html.escape(rendered_at)}</time>")
    if source_url:
        meta_parts.append(
            f'Source: <a href="{html.escape(source_url)}" '
            f'rel="noreferrer">{html.escape(source_url)}</a>'
        )
    meta = " · ".join(meta_parts)

    sections_html = "\n".join(_render_section(s) for s in doc.sections)

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{html.escape(title)}</title>\n"
        f'  <link rel="stylesheet" href="{_SHARED_CSS_PREFIX}/bootstrap.min.css">\n'
        f'  <link rel="stylesheet" href="{_SHARED_CSS_PREFIX}/style.min.css">\n'
        "  <style>"
        + _PAGE_STYLE
        + "  </style>\n"
        "</head>\n"
        "<body>\n"
        '<main class="x-doc">\n'
        f"  <h1>{html.escape(title)}</h1>\n"
        f'  <div class="x-meta">{meta}</div>\n'
        '  <div class="x-layout">\n'
        '    <nav class="x-toc" aria-label="Sections">\n'
        "      <h2>Contents</h2>\n"
        + _render_toc(doc.sections)
        + "\n    </nav>\n"
        '    <div class="x-body">\n'
        + sections_html
        + "\n    </div>\n"
        "  </div>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )
