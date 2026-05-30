"""Filter catalog parsing — walks ``filters.texi`` and emits one
:class:`FilterEntry` per ``@section`` documented under an audio/video/
multimedia filter chapter.

Each entry's ``args`` comes from the leading ``@table @option`` nested
inside the section (one row per filter argument). The rest of the section
content — including sub-sections like ``Examples`` / ``Commands`` —
flattens into the ``description`` list as Markdown paragraphs.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from .models import FilterEntry
from .texi_markdown import plain_text, render_block, render_paragraphs
from .texi_traversal import SECTION_TAGS, normalize_name, section_title


def _filter_type_for_heading(title: str) -> str | None:
    lower = title.lower()
    if "filter" not in lower and "source" not in lower and "sink" not in lower:
        return None
    if "audio" in lower:
        return "audio"
    if "video" in lower:
        return "video"
    if "multimedia" in lower or "mix" in lower:
        return "mixed"
    return None


def _filter_names_from_title(title: str) -> list[str]:
    # Strip trailing parens — e.g. "abuffer (source)" → "abuffer".
    base = title.split("(", 1)[0].strip()
    if not base:
        return []
    parts = [p.strip() for p in base.split(",") if p.strip()]
    names: list[str] = []
    for part in parts:
        # Sometimes the form is "aap algorithm" — keep only the leading token.
        token = part.split()[0].strip("-")
        normalized = normalize_name(token)
        if normalized:
            names.append(normalized)
    return names


def _filter_args_from_section(section: ET.Element) -> tuple[list[str], dict[str, list[str]]]:
    """Pull the @table @option args block (if any) at this section's level."""
    params: list[str] = []
    args: dict[str, list[str]] = {}
    # Only the section's direct @table (not nested inside another filter).
    for table in section.findall("table"):
        for entry in table.findall("tableentry"):
            for item in entry.findall("tableterm/item") + entry.findall("tableterm/itemx"):
                fmt = item.find("itemformat")
                raw = plain_text(fmt) if fmt is not None else plain_text(item)
                arg_name = _arg_name_from_text(raw)
                if not arg_name:
                    continue
                if arg_name not in args:
                    args[arg_name] = []
                    params.append(arg_name)
                body: list[str] = []
                for ti in entry.findall("tableitem"):
                    body.extend(render_paragraphs(ti))
                if body:
                    args[arg_name] = body
    return params, args


def _arg_name_from_text(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    # An item like "order @var{integer}" → first whitespace-separated token.
    head = raw.split()[0]
    head = head.split(",", 1)[0].strip()
    return normalize_name(head)


def _filter_description(section: ET.Element) -> list[str]:
    """Render all block content of ``section`` except the args @table."""
    paragraphs: list[str] = []
    # Skip the first @table @option (it's the args table, already extracted).
    skipped_args_table = False
    for child in section:
        if child.tag == "table" and not skipped_args_table:
            skipped_args_table = True
            continue
        if child.tag in SECTION_TAGS:
            # Sub-sections like "Examples"/"Commands" — flatten into desc.
            sub = render_paragraphs(child)
            if sub:
                title = section_title(child).strip()
                if title:
                    paragraphs.append(f"**{title}**")
                paragraphs.extend(sub)
            continue
        rendered = render_block(child)
        if rendered:
            paragraphs.append(rendered)
    return paragraphs


def parse_filters_xml(root: ET.Element) -> list[FilterEntry]:
    filters: list[FilterEntry] = []

    def visit(elem: ET.Element, type_: str, in_filter_chapter: bool) -> None:
        for child in elem:
            if child.tag not in SECTION_TAGS:
                visit(child, type_, in_filter_chapter)
                continue

            title = section_title(child)
            new_type = _filter_type_for_heading(title) or type_

            if child.tag == "chapter":
                if _filter_type_for_heading(title) is not None:
                    visit(child, new_type, True)
                else:
                    visit(child, new_type, False)
                continue

            if in_filter_chapter and child.tag == "section":
                names = _filter_names_from_title(title)
                if names:
                    params, args = _filter_args_from_section(child)
                    description = _filter_description(child)
                    filters.append(
                        FilterEntry(
                            name=names[0],
                            type=new_type,
                            aliases=names[1:],
                            params=params,
                            description=description,
                            args=args,
                        )
                    )
                # Don't descend — sub-sections (Commands, Examples) are
                # already merged into the filter's description.
                continue

            visit(child, new_type, in_filter_chapter)

    visit(root, "video", False)
    return filters
