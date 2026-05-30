"""AVCodec / AVFormat option parsing.

Covers four related catalogs whose entries are :class:`AVOptionEntry`
instances — distinct from driver options because applicability is tied
to *role tags* (``encoding`` / ``decoding`` / ``audio`` / ``video`` /
``subtitle`` for AVCodec; ``input`` / ``output`` for AVFormat) rather
than a global / input / output scope:

- :func:`parse_codec_options_xml` — the generic AVCodec chapter from
  ``codecs.texi``.
- :func:`parse_format_options_xml` — the generic AVFormat chapter from
  ``formats.texi``.
- :func:`parse_per_codec_options_xml` — codec-private options from
  ``encoders.texi`` / ``decoders.texi``.
- :func:`parse_per_format_options_xml` — muxer/demuxer-private options
  from ``muxers.texi`` / ``demuxers.texi``.

Plus :func:`merge_per_codec_options`, which unions the encoder-side and
decoder-side per-codec tables.

Reuses :func:`classify_value_type` from :mod:`.options_parser` plus the
shared ``iter_item_heads`` / ``render_entry_body`` scaffolding that the
driver-option builder also uses; reuses
:func:`codec_aliases_from_title` from :mod:`.codecs_parser`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from xml.etree import ElementTree as ET

from .codecs_parser import codec_aliases_from_title
from .models import AVOptionEntry
from .options_parser import (
    classify_value_type,
    iter_item_heads,
    render_entry_body,
)
from .texi_markdown import plain_text
from .texi_traversal import (
    SECTION_TAGS,
    normalize_name,
    section_anchor,
    section_title,
    trailing_anchor,
)

# Bare AVOption names use the same character class as driver options but
# without the required leading dashes.
_AV_OPTION_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9:._-]*)")

# Role tags recognized inside the ``(@emph{...})`` trailer of an AVOption
# ``@item`` line. Anything outside this set is ignored — keeps unrelated
# parentheticals from polluting the role list.
_AV_CODEC_ROLES = {
    "encoding", "decoding", "audio", "video", "subtitle", "subtitles",
}
_AV_FORMAT_ROLES = {"input", "output"}


def _av_option_roles(fmt: ET.Element, allowed: set[str]) -> list[str]:
    """Pull role tags from the ``(@emph{role,kinds})`` trailer of an AVOption
    item. Tokens are split on ``,`` and ``/`` (the docs use both) and
    intersected with the allowed-roles set for the layer being parsed.
    """
    roles: list[str] = []
    seen: set[str] = set()
    for emph in fmt.iter("emph"):
        text = plain_text(emph).strip().lower()
        if not text:
            continue
        for token in re.split(r"[,/]", text):
            tok = token.strip()
            if not tok or tok in seen or tok not in allowed:
                continue
            seen.add(tok)
            roles.append(tok)
    return roles


def _av_option_from_entry(
    entry: ET.Element, anchor: str, allowed_roles: set[str]
) -> AVOptionEntry | None:
    """Build an :class:`AVOptionEntry` from one ``<tableentry>`` inside an
    AVCodec / AVFormat option table.

    Names come without a leading ``-`` in the docs (``@item b @var{integer}``);
    the leading dash is added on emit so the SPA can resolve the option from
    the command-line form (``-b``). Items with no ``<itemformat>`` child are
    skipped — bare-text rows aren't AVOption shape.
    """
    heads = list(iter_item_heads(entry))
    if not heads:
        return None

    names: list[str] = []
    value_type = "none"
    signatures: list[str] = []
    roles: list[str] = []

    for head in heads:
        if head.fmt is None:
            continue
        match = _AV_OPTION_NAME_RE.match(head.head_for_names)
        if match:
            names.append(f"-{normalize_name(match.group(1))}")

        classified = classify_value_type(head.fmt)
        if classified is not None and value_type == "none":
            value_type = classified

        if head.signature:
            signatures.append(head.signature)

        for role in _av_option_roles(head.fmt, allowed_roles):
            if role not in roles:
                roles.append(role)

    if not names:
        return None

    description_paragraphs, values, value_type = render_entry_body(entry, value_type)

    # Normalize role spelling: the docs use both ``subtitle`` and ``subtitles``;
    # collapse to the singular so callers can match a single key.
    roles = ["subtitle" if r == "subtitles" else r for r in roles]

    return AVOptionEntry(
        name=names[0],
        aliases=names[1:],
        value_type=value_type,
        values=values,
        description=description_paragraphs,
        anchor=anchor,
        signature=signatures,
        roles=roles,
    )


def _parse_av_options(
    root: ET.Element, allowed_roles: set[str]
) -> list[AVOptionEntry]:
    """Walk an AVOption texi document (``codecs.texi``, ``formats.texi``)
    and return one entry per ``@item`` in the document's primary chapter.

    Only the *first* ``<chapter>`` is scanned — both ``codecs.texi`` and
    ``formats.texi`` ``@include`` per-codec / per-muxer files at the end
    (``decoders.texi``, ``encoders.texi`` / ``muxers.texi``,
    ``demuxers.texi``), and those add additional chapters that belong to the
    later per-component passes (S4/S5), not to the generic AVOption pool.

    Within the chapter, only direct-child ``@table @option`` blocks are
    harvested. Anchors fall back to the chapter's own anchor.
    """
    entries: list[AVOptionEntry] = []
    chapter = root.find("chapter")
    if chapter is None:
        return entries

    chapter_anchor = ""
    # Prefer an explicit ``@anchor{}`` placed immediately inside the chapter
    # (codecs.texi opens with ``@anchor{codec-options}``); fall back to the
    # makeinfo-encoded chapter title for older tags missing the anchor.
    inner_anchor = chapter.find("anchor")
    if inner_anchor is not None:
        chapter_anchor = (inner_anchor.get("name") or "").strip()
    if not chapter_anchor:
        chapter_anchor = section_anchor(chapter, section_title(chapter))

    pending_anchor: str | None = None
    for child in chapter:
        if child.tag == "anchor":
            name = (child.get("name") or "").strip()
            if name:
                pending_anchor = name
            continue
        if child.tag != "table":
            pending_anchor = None
            continue
        if child.get("commandarg") != "option":
            pending_anchor = None
            continue
        table_anchor = pending_anchor or chapter_anchor
        pending_anchor = None
        inner_pending: str | None = None
        for entry in child.findall("tableentry"):
            entry_anchor = inner_pending or table_anchor
            option = _av_option_from_entry(entry, entry_anchor, allowed_roles)
            if option is not None:
                entries.append(option)
            inner_pending = trailing_anchor(entry)

    return entries


def parse_codec_options_xml(root: ET.Element) -> list[AVOptionEntry]:
    """Walk ``codecs.texi`` and emit one entry per documented generic
    AVCodec option. Each entry's ``roles`` field carries the
    ``(@emph{encoding,audio,video})`` tags that describe when the option
    applies.
    """
    return _parse_av_options(root, _AV_CODEC_ROLES | {"subtitles"})


def parse_format_options_xml(root: ET.Element) -> list[AVOptionEntry]:
    """Walk ``formats.texi`` and emit one entry per generic AVFormat option,
    with ``roles`` drawn from the ``(@emph{input/output})`` tag.
    """
    return _parse_av_options(root, _AV_FORMAT_ROLES)


# === Per-codec / per-format private options ========================
#
# encoders.texi / decoders.texi document codec-private AVOptions ("the libx264
# preset", "the aom-av1 cq-level") that don't live in either codecs.texi's
# generic chapter or in ffmpeg.texi's driver-options pool. Each ``@section``
# in those files is a codec (or codec family); options are listed in one or
# more ``@table @option`` blocks reachable from the section (an ``Options``
# subsection in the common case, but several encoders use other titles —
# "Private Options for X", "Metadata Control Options" — so we collect every
# descendant option table indiscriminately).


def _per_codec_option_from_entry(
    entry: ET.Element, anchor: str, side: str
) -> AVOptionEntry | None:
    """Build one :class:`AVOptionEntry` for a per-codec option ``<tableentry>``.

    Differs from :func:`_av_option_from_entry` in three ways:

    1. ``@item`` heads sometimes carry a leading ``-`` (ac3's
       ``@item -per_frame_metadata @var{boolean}``); strip it before name
       matching so the canonical form (``-per_frame_metadata``) survives.
    2. The ``(@emph{x264-equivalent})`` parenthetical is *not* a role tag — it
       names the upstream library's equivalent option for migration help.
       Roles for per-codec options are unambiguous: ``["encoder"]`` for
       encoders.texi entries, ``["decoder"]`` for decoders.texi entries.
    3. Most documented options take string values even without an explicit
       ``@var{type}`` (encoders.texi rarely uses type hints — libx264's
       ``@item preset (@emph{preset})`` is the rule, not the exception).
       Default to ``"string"`` here; tighten only when a ``@var`` is present
       *and* hints at a stronger scalar type.
    """
    heads = list(iter_item_heads(entry))
    if not heads:
        return None

    names: list[str] = []
    value_type = "string"  # encoders.texi default — most items take values.
    signatures: list[str] = []

    for head in heads:
        if head.fmt is None:
            continue
        # ac3 / a few others write ``@item -per_frame_metadata @var{boolean}``;
        # strip the leading dash before matching so the regex (which expects
        # an alpha lead char) succeeds either way.
        name_text = head.head_for_names.lstrip("-")
        match = _AV_OPTION_NAME_RE.match(name_text)
        if match:
            names.append(f"-{normalize_name(match.group(1))}")

        classified = classify_value_type(head.fmt)
        if classified is not None and classified != "string":
            value_type = classified

        if head.signature:
            signatures.append(head.signature)

    if not names:
        return None

    description_paragraphs, values, value_type = render_entry_body(entry, value_type)

    return AVOptionEntry(
        name=names[0],
        aliases=names[1:],
        value_type=value_type,
        values=values,
        description=description_paragraphs,
        anchor=anchor,
        signature=signatures,
        roles=[side],
    )


def _av_option_richness(entry: AVOptionEntry) -> int:
    """Score how "fully documented" an option entry is.

    Some doc sections list the same flag multiple times — e.g. muxers.texi's
    MOV ``Fragmentation`` subsection has shorthand items like
    ``@item movflags +frag_keyframe`` that document one value of an option
    later fully specified by ``@item movflags @var{flags}`` plus a value
    table. Richness lets the section-level dedupe keep the proper entry
    instead of the partial shorthand.

    Higher = more informative. Ranked signals: explicit value table > a
    scalar/flags/bool ``valueType`` (anything other than the default
    ``"string"`` / ``"none"``) > longer description.
    """
    score = 0
    if entry.values:
        score += 1000
    if entry.value_type not in ("string", "none"):
        score += 100
    score += len(entry.description)
    return score


def _dedupe_per_section(entries: list[AVOptionEntry]) -> list[AVOptionEntry]:
    """Collapse same-name entries within a single section to the richest
    documented variant; preserves first-seen order.
    """
    by_name: dict[str, AVOptionEntry] = {}
    order: list[str] = []
    for entry in entries:
        existing = by_name.get(entry.name)
        if existing is None:
            by_name[entry.name] = entry
            order.append(entry.name)
            continue
        if _av_option_richness(entry) > _av_option_richness(existing):
            by_name[entry.name] = entry
    return [by_name[n] for n in order]


def _collect_per_codec_options(
    section: ET.Element, side: str
) -> list[AVOptionEntry]:
    """Walk every ``@table @option`` block reachable from ``section`` and
    emit a private-option entry for each ``<tableentry>``.

    Doesn't gate on the subsection title — encoders.texi uses "Options",
    "Private Options for X", "Metadata Control Options", and "Shared
    options" / "Private options" splits depending on the codec. The
    section's own ``@anchor{}`` (or makeinfo-derived anchor) is used for
    every option; per-option anchors aren't worth tracking at this layer.
    """
    this_section_anchor = section_anchor(section, section_title(section))
    out: list[AVOptionEntry] = []
    for table in section.iter("table"):
        if table.get("commandarg") != "option":
            continue
        for entry in table.findall("tableentry"):
            option = _per_codec_option_from_entry(entry, this_section_anchor, side)
            if option is not None:
                out.append(option)
    return _dedupe_per_section(out)


def parse_per_codec_options_xml(
    root: ET.Element, side: str, known_codec_names: set[str]
) -> dict[str, list[AVOptionEntry]]:
    """Walk ``encoders.texi`` / ``decoders.texi`` and return, per codec,
    the list of private options documented in its ``@section``.

    ``side`` is the layer tag attached to every emitted option's ``roles``:
    ``"encoder"`` or ``"decoder"``.

    ``known_codec_names`` filters section titles down to those whose
    aliases actually match a codec already discovered in ``codecs.texi`` +
    ``allcodecs.c``. Family / umbrella sections that don't match (e.g.
    "QSV Encoders", "VAAPI encoders") are dropped — their constituent
    codecs (``h264_qsv``, ``hevc_qsv``, …) get no private options for v1.
    """
    by_codec: dict[str, list[AVOptionEntry]] = {}

    def visit(elem: ET.Element, in_codec_chapter: bool) -> None:
        for child in elem:
            if child.tag not in SECTION_TAGS:
                continue
            title = section_title(child)

            if child.tag == "chapter":
                lower = title.lower()
                # "Encoders", "Audio Encoders", "Video Encoders", … and the
                # same with "Decoders". An umbrella "@chapter Encoders" with
                # no qualifier counts too.
                if "encoder" in lower or "decoder" in lower:
                    visit(child, True)
                else:
                    visit(child, False)
                continue

            if not in_codec_chapter or child.tag != "section":
                # Subsections of an unmatched parent contribute nothing on
                # their own — only ``@section`` granularity is treated as a
                # codec boundary.
                continue

            aliases = codec_aliases_from_title(title)
            matched = [a for a in aliases if a in known_codec_names]
            if not matched:
                continue
            options = _collect_per_codec_options(child, side)
            if not options:
                continue
            for name in matched:
                by_codec.setdefault(name, []).extend(options)

    visit(root, False)
    return by_codec


def _section_family_members(
    section: ET.Element, known: set[str]
) -> list[str]:
    """Family-section helper: read the leading ``@table @samp`` block (if any)
    that enumerates constituent entities by name, and return those whose
    first-token name matches the known set.

    Used by muxer/demuxer per-section walking to handle "MOV/MPEG-4/ISOMBFF
    muxers" → ``{3gp, 3g2, f4v, ipod, ismv, mov, mp4, psp}``. The samp table
    must appear before the section's first subsection — otherwise it's
    probably an enum value table embedded in an option description, not a
    family roster.
    """
    members: list[str] = []
    seen: set[str] = set()
    for child in section:
        if child.tag in SECTION_TAGS:
            break
        if child.tag != "table" or child.get("commandarg") != "samp":
            continue
        for term in child.findall("tableentry/tableterm"):
            for item in term.findall("item") + term.findall("itemx"):
                fmt = item.find("itemformat")
                raw = plain_text(fmt) if fmt is not None else plain_text(item)
                head = raw.split("(", 1)[0].strip()
                tokens = head.split()
                if not tokens:
                    continue
                token = normalize_name(tokens[0].rstrip(",:;."))
                if token and token in known and token not in seen:
                    seen.add(token)
                    members.append(token)
        # Only consult the first leading @samp table per section.
        return members
    return members


def parse_per_format_options_xml(
    root: ET.Element, side: str, known_format_names: set[str]
) -> dict[str, list[AVOptionEntry]]:
    """Walk ``muxers.texi`` / ``demuxers.texi`` and return, per muxer/demuxer,
    the list of private options documented in its ``@section``.

    Mirrors :func:`parse_per_codec_options_xml` but with two differences:

    1. The chapter filter is ``"muxer"`` / ``"demuxer"``.
    2. Many sections are *family* containers (``@section MOV/MPEG-4/ISOMBFF
       muxers``) whose title doesn't match a single muxer name. When the
       section opens with an ``@table @samp`` enumerating constituent muxers,
       the options are attached to every enumerated member that exists in
       the known set — recovering coverage for 3gp/3g2/f4v/ipod/ismv/mp4/psp
       that the title alone would have missed.

    ``side`` is the layer tag (``"muxer"`` or ``"demuxer"``) attached to each
    emitted option's ``roles``.
    """
    by_name: dict[str, list[AVOptionEntry]] = {}

    def visit(elem: ET.Element, in_chapter: bool) -> None:
        for child in elem:
            if child.tag not in SECTION_TAGS:
                continue
            title = section_title(child)

            if child.tag == "chapter":
                lower = title.lower()
                if "muxer" in lower or "demuxer" in lower:
                    visit(child, True)
                else:
                    visit(child, False)
                continue

            if not in_chapter or child.tag != "section":
                continue

            aliases = codec_aliases_from_title(title)
            matched: list[str] = []
            seen: set[str] = set()
            for a in aliases:
                if a in known_format_names and a not in seen:
                    seen.add(a)
                    matched.append(a)
            for m in _section_family_members(child, known_format_names):
                if m not in seen:
                    seen.add(m)
                    matched.append(m)
            if not matched:
                continue

            options = _collect_per_codec_options(child, side)
            if not options:
                continue
            for name in matched:
                by_name.setdefault(name, []).extend(options)

    visit(root, False)
    return by_name


def merge_per_codec_options(
    encoder_side: dict[str, list[AVOptionEntry]],
    decoder_side: dict[str, list[AVOptionEntry]],
) -> dict[str, list[AVOptionEntry]]:
    """Combine encoder-side and decoder-side option tables for the same
    codec. Options with the same name across both sides have their
    ``roles`` lists unioned (the encoder-side entry's other fields win —
    encoders.texi tends to be the more thoroughly documented half).
    """
    merged: dict[str, list[AVOptionEntry]] = {}
    for name, options in encoder_side.items():
        merged[name] = list(options)
    for name, options in decoder_side.items():
        if name not in merged:
            merged[name] = list(options)
            continue
        existing_by_name = {o.name: i for i, o in enumerate(merged[name])}
        for option in options:
            if option.name in existing_by_name:
                idx = existing_by_name[option.name]
                prev = merged[name][idx]
                combined_roles = list(prev.roles)
                for r in option.roles:
                    if r not in combined_roles:
                        combined_roles.append(r)
                merged[name][idx] = replace(prev, roles=combined_roles)
            else:
                merged[name].append(option)
    return merged
