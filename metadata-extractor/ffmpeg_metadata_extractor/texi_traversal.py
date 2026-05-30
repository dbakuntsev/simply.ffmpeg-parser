"""Section / anchor utilities shared by every parser variant.

Resolves section titles, derives or reads HTML anchors (mirroring the
encoding ``makeinfo --html`` applies when there is no explicit
``@anchor{}``), and normalizes option/codec names.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from .texi_markdown import plain_text

SECTION_TAGS = {
    "chapter", "section", "subsection", "subsubsection",
    "appendix", "appendixsec", "appendixsubsec", "unnumbered",
    "unnumberedsec", "unnumberedsubsec", "top",
}


def section_title(section: ET.Element) -> str:
    title = section.find("sectiontitle")
    if title is None:
        return ""
    return plain_text(title).strip()


def normalize_name(value: str) -> str:
    return value.strip().lower()


def trailing_anchor(entry: ET.Element) -> str | None:
    """Return the ``@anchor{}`` name placed between this entry and the next.

    Texi convention places fine-grained anchors before an ``@item`` (e.g.
    ``@anchor{filter_option}\\n@item -filter ...``). Makeinfo's XML emits that
    anchor as a trailing child of the *previous* ``<tableitem>``, so it has
    to be carried forward to the next entry. Returns ``None`` when no
    trailing anchor is present.
    """
    last_item = None
    for child in entry:
        if child.tag == "tableitem":
            last_item = child
    if last_item is None:
        return None
    children = list(last_item)
    if not children:
        return None
    last = children[-1]
    if last.tag != "anchor":
        return None
    name = (last.get("name") or "").strip()
    return name or None


def section_anchor(section: ET.Element, title: str) -> str:
    """Resolve a section's HTML anchor.

    An explicit ``@anchor{...}`` either inside the section (as a first child)
    or *immediately* preceding it in the source survives in makeinfo's XML as
    a sibling anchor or an in-section anchor — both are handled by the
    caller, which scans for preceding-sibling anchors when walking direct
    children. When no explicit anchor is provided, derive one from the title
    using :func:`makeinfo_anchor`.
    """
    inner = section.find("anchor")
    if inner is not None and inner.get("name"):
        return (inner.get("name") or "").strip()
    return makeinfo_anchor(title)


def makeinfo_anchor(title: str) -> str:
    """Encode ``title`` the way ``makeinfo --html`` derives a section anchor.

    Empirically (cross-checked against ``ffmpeg-all.html``): preserve ASCII
    letters/digits and ``_``, map ASCII space to ``-``, encode every other
    byte (including existing ``-``) as ``_XXXX`` (four hex digits of its
    code point). The rule has to encode ``-`` because makeinfo uses ``-`` as
    its escape for spaces; otherwise the mapping wouldn't round-trip.
    Example: ``"MOV/MPEG-4/ISOMBFF muxers"`` ⇒
    ``"MOV_002fMPEG_002d4_002fISOMBFF-muxers"``.
    """
    out: list[str] = []
    for ch in title:
        if ch == " ":
            out.append("-")
        elif ch == "_" or (ch.isascii() and ch.isalnum()):
            out.append(ch)
        else:
            out.append(f"_{ord(ch):04x}")
    return "".join(out)


def anchor_for_section(section_anchor_value: str | None, fallback_title: str) -> str:
    """The HTML anchor that links to this entity.

    Explicit ``@anchor{...}`` wins — its value already comes through the XML
    in the encoded form makeinfo will use in the HTML output (e.g.
    ``@anchor{raw muxers}`` ⇒ ``raw-muxers``). Otherwise we derive the
    auto-anchor from the section title using the same encoding makeinfo
    applies when it generates the HTML id.
    """
    if section_anchor_value:
        return section_anchor_value
    return makeinfo_anchor(fallback_title)
