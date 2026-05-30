"""Catalog parsing for the six single-entity texis: ``demuxers.texi``,
``muxers.texi``, ``protocols.texi``, ``bitstream_filters.texi``,
``indevs.texi``, ``outdevs.texi``.

Each entry produced is a :class:`NamedEntry` (``name``, ``aliases``,
``anchor``, ``description``). The shape is intentionally simple — the SPA
uses these for value-level enrichment of options like ``-f``, ``-bsf``,
and protocol URIs.

Some sections are *grouped* — their heading is descriptive prose
(``MOV/MPEG-4/ISOMBFF muxers``) and the constituents live as ``@samp``
items inside the section; :func:`_named_entries_from_samp_table` extracts
those. Single-entity sections (``flv, live_flv, kux`` or ``mov/mp4/3gp``)
go through :func:`_named_aliases_from_title`.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .models import NamedEntry
from .texi_markdown import plain_text, render_block, render_paragraphs
from .texi_traversal import SECTION_TAGS, anchor_for_section, normalize_name, section_title


# A "grouped" section is a `@section` whose heading is descriptive prose rather
# than a single format/protocol name — e.g. ``Raw muxers`` or
# ``MOV/MPEG-4/ISOMBFF muxers``. Its child `@table @samp` lists the actual
# entities (mp4, mov, 3gp, …). The discriminator: presence of a space in the
# heading. Single-entity headings like ``concat`` or ``flv, live_flv, kux`` or
# ``mov/mp4/3gp`` never contain a space.
def _is_grouped_section_title(title: str) -> bool:
    return " " in title.strip()


# Split a single-entity section heading into a primary name + aliases. The
# heading conventions in the catalog texis are: comma-separated (``flv,
# live_flv, kux``), slash-separated (``mov/mp4/3gp``), or just one token.
_NAMED_TITLE_SPLIT = re.compile(r"\s*[,/]\s*")


def _named_aliases_from_title(title: str) -> list[str]:
    parts = [p.strip() for p in _NAMED_TITLE_SPLIT.split(title) if p.strip()]
    out: list[str] = []
    for part in parts:
        token = part.split("(", 1)[0].strip()
        normalized = normalize_name(token)
        if normalized:
            out.append(normalized)
    return out


def _named_description(section: ET.Element) -> list[str]:
    """Render a section's block content as Markdown paragraphs.

    Subsections (Examples, Options, Syntax, Background, …) are flattened in,
    matching the filter parser's treatment.
    """
    paragraphs: list[str] = []
    for child in section:
        if child.tag in SECTION_TAGS:
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


def _named_entries_from_samp_table(
    section: ET.Element, parent_anchor: str
) -> list[NamedEntry]:
    """Extract entities from each ``@table @samp`` ``@item`` inside ``section``.

    Used for grouped sections (e.g. ``MOV/MPEG-4/ISOMBFF muxers``) where the
    individual format names live as ``@samp`` items rather than as their own
    sections. The anchor for each falls back to ``parent_anchor`` because
    @samp items don't carry their own anchor; the section-level link is the
    best target makeinfo emits.
    """
    out: list[NamedEntry] = []
    for table in section.findall("table"):
        if table.get("commandarg") not in ("samp", "option"):
            continue
        for entry in table.findall("tableentry"):
            terms = entry.findall("tableterm/item") + entry.findall("tableterm/itemx")
            if not terms:
                continue
            names: list[str] = []
            aliases: list[str] = []
            for item in terms:
                fmt = item.find("itemformat")
                raw = (
                    plain_text(fmt) if fmt is not None else plain_text(item)
                ).strip()
                if not raw:
                    continue
                # Pull a single token from the item head; ignore any trailing
                # @emph{audio}/@emph{video} marker and parenthetical aliases —
                # but capture parenthetical aliases as additional names.
                head = raw.split()[0].strip(",")
                normalized = normalize_name(head)
                if normalized:
                    names.append(normalized)
                paren_match = re.search(r"\(([^)]+)\)", raw)
                if paren_match:
                    for piece in paren_match.group(1).split(","):
                        alias = normalize_name(piece.strip())
                        if alias:
                            aliases.append(alias)
            if not names:
                continue
            description: list[str] = []
            for body in entry.findall("tableitem"):
                description.extend(render_paragraphs(body))
            primary = names[0]
            all_aliases = list(dict.fromkeys(names[1:] + aliases))
            all_aliases = [a for a in all_aliases if a != primary]
            out.append(
                NamedEntry(
                    name=primary,
                    aliases=all_aliases,
                    anchor=parent_anchor,
                    description=description,
                )
            )
    return out


def _collect_named_sections(
    container: ET.Element, chapter_predicate, sink: list[NamedEntry]
) -> None:
    for chapter in container.iter("chapter"):
        title = section_title(chapter)
        if not chapter_predicate(title):
            continue
        # Walk direct children so we can pair preceding `<anchor>` siblings
        # with each `<section>` (the texis put `@anchor{x}` on the line above
        # the section, which makeinfo emits as a sibling, not a child).
        pending_anchor: str | None = None
        for child in chapter:
            if child.tag == "anchor":
                pending_anchor = (child.get("name") or "").strip() or pending_anchor
                continue
            if child.tag != "section":
                continue

            this_section_title = section_title(child).strip()
            inner_anchor = child.find("anchor")
            anchor_value = pending_anchor
            if inner_anchor is not None and inner_anchor.get("name"):
                anchor_value = (inner_anchor.get("name") or "").strip()
            pending_anchor = None

            if _is_grouped_section_title(this_section_title):
                # The group section itself doesn't represent a single entity.
                # Use its anchor (or section title) as the fallback link for
                # each @samp item it contains.
                fallback = anchor_for_section(anchor_value, this_section_title)
                sink.extend(_named_entries_from_samp_table(child, fallback))
                continue

            aliases = _named_aliases_from_title(this_section_title)
            if not aliases:
                continue
            primary = aliases[0]
            sink.append(
                NamedEntry(
                    name=primary,
                    aliases=aliases[1:],
                    anchor=anchor_for_section(anchor_value, this_section_title),
                    description=_named_description(child),
                )
            )


def parse_demuxers_xml(root: ET.Element) -> list[NamedEntry]:
    out: list[NamedEntry] = []
    _collect_named_sections(root, lambda t: t.strip().lower() == "demuxers", out)
    return out


def parse_muxers_xml(root: ET.Element) -> list[NamedEntry]:
    out: list[NamedEntry] = []
    _collect_named_sections(root, lambda t: t.strip().lower() == "muxers", out)
    return out


def parse_protocols_xml(root: ET.Element) -> list[NamedEntry]:
    out: list[NamedEntry] = []
    # ``Protocols`` is the chapter that lists individual protocols. The
    # ``Protocol Options`` chapter at the top is about global protocol
    # options, not specific protocols — skip it.
    _collect_named_sections(root, lambda t: t.strip().lower() == "protocols", out)
    return out


def parse_bitstream_filters_xml(root: ET.Element) -> list[NamedEntry]:
    out: list[NamedEntry] = []
    _collect_named_sections(
        root, lambda t: t.strip().lower() == "bitstream filters", out
    )
    return out


def parse_input_devices_xml(root: ET.Element) -> list[NamedEntry]:
    # Input devices share the ``-f`` flag with demuxers (libavdevice surfaces
    # them as demuxers at runtime), so they're merged into the demuxers bundle
    # downstream — see ``_extract_named`` calls in ``extractor.py``.
    out: list[NamedEntry] = []
    _collect_named_sections(
        root, lambda t: t.strip().lower() == "input devices", out
    )
    return out


def parse_output_devices_xml(root: ET.Element) -> list[NamedEntry]:
    out: list[NamedEntry] = []
    _collect_named_sections(
        root, lambda t: t.strip().lower() == "output devices", out
    )
    return out
