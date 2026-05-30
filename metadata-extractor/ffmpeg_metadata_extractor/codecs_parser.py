"""Codec catalog parsing — section walker over ``codecs.texi`` plus the
``libavcodec/allcodecs.c`` symbol-table merge.

``codecs.texi`` gives the documented codec list (with ``type`` and
``aliases``); ``allcodecs.c`` is scanned for ``ff_<name>_(encoder|decoder)``
symbols to OR in the encoder/decoder availability flags. Names that only
appear in the C source surface as ``type="video"`` (the historical
default — no name-based type inference).

:func:`codec_aliases_from_title` is public to the package because the
per-codec / per-format option parsers reuse the same title-splitting
convention (``@section flv, live_flv, kux``, ``@section mov/mp4/3gp``).
"""

from __future__ import annotations

import re
from dataclasses import replace
from xml.etree import ElementTree as ET

from .models import CodecEntry
from .texi_traversal import (
    SECTION_TAGS,
    normalize_name,
    section_anchor,
    section_title,
)


def _codec_type_for_heading(title: str) -> str | None:
    """Switch the current codec type when walking codecs.texi headings."""
    lower = title.lower()
    if "video" in lower:
        return "video"
    if "audio" in lower:
        return "audio"
    if "subtitle" in lower:
        return "subtitle"
    return None


def _codec_role_for_heading(title: str) -> tuple[bool, bool] | None:
    """Return ``(encoder, decoder)`` if ``title`` declares a codec role."""
    lower = title.lower()
    enc = "encoder" in lower
    dec = "decoder" in lower
    if enc and not dec:
        return (True, False)
    if dec and not enc:
        return (False, True)
    return None


_CODEC_TITLE_SPLIT = re.compile(r"\s*(?:,|\s+and\s+|\s*/\s*)\s*")


def codec_aliases_from_title(title: str) -> list[str]:
    parts = [p.strip() for p in _CODEC_TITLE_SPLIT.split(title) if p.strip()]
    out: list[str] = []
    for part in parts:
        # Drop trailing parens/qualifiers — e.g. "rawvideo (raw video)" → "rawvideo".
        token = part.split("(", 1)[0].strip().split()[0] if part.split() else ""
        normalized = normalize_name(token)
        if normalized:
            out.append(normalized)
    return out


def parse_codecs_xml(root: ET.Element) -> list[CodecEntry]:
    codecs: list[CodecEntry] = []

    def visit(elem: ET.Element, type_: str, role: tuple[bool, bool], in_codec_chapter: bool) -> None:
        pending_anchor: str | None = None
        for child in elem:
            if child.tag == "anchor":
                name = (child.get("name") or "").strip()
                if name:
                    pending_anchor = name
                continue
            if child.tag not in SECTION_TAGS:
                visit(child, type_, role, in_codec_chapter)
                pending_anchor = None
                continue

            title = section_title(child)
            new_type = _codec_type_for_heading(title) or type_
            new_role = _codec_role_for_heading(title) or role
            this_anchor = pending_anchor or section_anchor(child, title)
            pending_anchor = None

            is_codec_section = False
            if child.tag == "chapter":
                lower = title.lower()
                # "Decoders", "Encoders", "Video Decoders", etc.
                if "decoder" in lower or "encoder" in lower:
                    in_codec_chapter = True
                    role = new_role
                    type_ = new_type
                    visit(child, type_, role, True)
                    continue
                # Non-codec chapter (e.g. "Codec Options") — descend but don't
                # treat its sections as codec entries.
                visit(child, new_type, new_role, False)
                continue

            if in_codec_chapter and child.tag == "section":
                # A `<section>` immediately under a codec chapter is a codec
                # entry. Skip sections that look like grouped sub-headings.
                lower = title.lower()
                if "decoder" in lower or "encoder" in lower:
                    # E.g. "QSV Decoders" — descend, treat children as codecs.
                    visit(child, new_type, new_role, True)
                    continue
                aliases = codec_aliases_from_title(title)
                if aliases:
                    codecs.append(
                        CodecEntry(
                            name=aliases[0],
                            type=new_type,
                            aliases=aliases[1:],
                            encoder=new_role[0],
                            decoder=new_role[1],
                            anchor=this_anchor,
                        )
                    )
                is_codec_section = True

            # Descend regardless — sections can have meaningful sub-sections.
            visit(child, new_type, new_role, in_codec_chapter and not is_codec_section)

    visit(root, "video", (False, False), False)
    return codecs


def parse_codecs_c(text: str) -> dict[str, dict[str, bool]]:
    entries: dict[str, dict[str, bool]] = {}
    for match in re.finditer(r"ff_([a-z0-9_]+)_(encoder|decoder)", text):
        name = normalize_name(match.group(1))
        kind = match.group(2)
        if name not in entries:
            entries[name] = {"encoder": False, "decoder": False}
        entries[name][kind] = True
    return entries


def merge_codec_flags(
    base: list[CodecEntry], flags: dict[str, dict[str, bool]]
) -> list[CodecEntry]:
    merged: list[CodecEntry] = []
    for codec in base:
        info = flags.get(codec.name)
        if info:
            merged.append(
                replace(
                    codec,
                    encoder=codec.encoder or info.get("encoder", False),
                    decoder=codec.decoder or info.get("decoder", False),
                )
            )
        else:
            merged.append(codec)
    return merged
