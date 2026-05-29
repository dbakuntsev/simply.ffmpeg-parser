"""Parse FFmpeg metadata from the XML emitted by ``makeinfo --xml``.

The texi sources are processed by :mod:`.texi_xml`, which returns an
ElementTree root. This module walks that tree to produce
:class:`OptionEntry` / :class:`CodecEntry` / :class:`FilterEntry` records.

Description fields are rendered as a list of Markdown paragraph strings —
the same shape the SPA already consumes.
"""

from __future__ import annotations

import re
from dataclasses import replace
from xml.etree import ElementTree as ET

from .models import AVOptionEntry, CodecEntry, FilterEntry, NamedEntry, OptionEntry


# === Markdown rendering of makeinfo XML ============================

_MD_SPECIALS = r"\`*_[]"


def _md_escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _MD_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


# Inline tags that render as a Markdown code span.
_CODE_TAGS = {
    "code", "command", "option", "samp", "file", "kbd", "key",
    "verb", "env", "t", "indicateurl",
}
# Inline tags that render as Markdown emphasis (italic).
_EM_TAGS = {"var", "emph", "i", "cite", "dfn"}
# Inline tags that render as Markdown strong (bold).
_STRONG_TAGS = {"strong", "b"}
# Pure passthrough wrappers — render content as-is.
_PASSTHROUGH_TAGS = {
    "math", "asis", "w", "sc", "sansserif", "r", "value",
    "itemformat", "para", "phrase",
}
# Inline tags whose content should be dropped entirely.
_DROP_INLINE_TAGS = {
    "anchor", "indexterm", "hyphenation", "errormsg",
    "set", "clear", "comment", "c",
}


def _plain_text(elem: ET.Element) -> str:
    """Recursively collect the text content of ``elem`` (no Markdown markup)."""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.tag in _DROP_INLINE_TAGS:
            pass
        else:
            parts.append(_plain_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _wrap_code(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def _render_inline(elem: ET.Element) -> str:
    """Render the inline (mixed) content of ``elem`` as Markdown."""
    parts: list[str] = []
    if elem.text:
        parts.append(_md_escape(elem.text))
    for child in elem:
        parts.append(_render_inline_child(child))
        if child.tail:
            parts.append(_md_escape(child.tail))
    return "".join(parts)


def _render_inline_child(elem: ET.Element) -> str:
    tag = elem.tag
    if tag in _DROP_INLINE_TAGS:
        return ""
    if tag in _CODE_TAGS:
        return _wrap_code(_plain_text(elem))
    if tag in _EM_TAGS:
        inner = _render_inline(elem).strip()
        return f"*{inner}*" if inner else ""
    if tag in _STRONG_TAGS:
        inner = _render_inline(elem).strip()
        return f"**{inner}**" if inner else ""
    if tag in _PASSTHROUGH_TAGS:
        return _render_inline(elem)
    if tag in ("uref", "url"):
        return _render_uref(elem)
    if tag == "email":
        return _render_email(elem)
    if tag in ("ref", "xref", "pxref", "inforef"):
        return _render_xref(elem, tag)
    if tag == "linebreak":
        return "\n"
    # Unknown element — recurse so we don't drop meaningful text.
    return _render_inline(elem)


def _child_text(elem: ET.Element | None, child_tag: str) -> str:
    if elem is None:
        return ""
    child = elem.find(child_tag)
    if child is None:
        return ""
    return _plain_text(child).strip()


def _render_uref(elem: ET.Element) -> str:
    url = _child_text(elem, "urefurl")
    desc = _child_text(elem, "urefdesc") or _child_text(elem, "urefreplacement")
    if not url:
        # @url{some text} without a real URL — just return the text.
        return _md_escape(_plain_text(elem).strip())
    if not desc or desc == url:
        return f"<{url}>"
    return f"[{_md_escape(desc)}]({url})"


def _render_email(elem: ET.Element) -> str:
    addr = _child_text(elem, "emailaddress")
    name = _child_text(elem, "emailname")
    if not addr:
        return _md_escape(_plain_text(elem).strip())
    if not name or name == addr:
        return f"<{addr}>"
    return f"[{_md_escape(name)}](mailto:{addr})"


def _render_xref(elem: ET.Element, tag: str) -> str:
    label = (
        _child_text(elem, "xrefprinteddesc")
        or _child_text(elem, "xrefinfoname")
        or _child_text(elem, "xrefprintedname")
        or _child_text(elem, "xrefnodename")
    )
    if not label:
        return ""
    rendered = f"*{_md_escape(label)}*"
    if tag == "xref":
        return f"See {rendered}"
    if tag == "pxref":
        return f"see {rendered}"
    return rendered


# --- Block rendering ----------------------------------------------------

# Tags inside a section/chapter that are decorative or layout-only.
_BLOCK_SKIP_TAGS = {
    "sectiontitle", "anchor", "indexterm", "noindent", "page", "sp",
    "menu", "detailmenu", "direntry", "copying", "titlepage",
    "printindex", "shorttitlepage", "subtitle", "author",
    "itemprepend", "beforefirstitem", "formattingcommand", "filename",
    "setfilename", "settitle", "set", "clear", "comment", "c",
    "node", "nodename", "nodenext", "nodeprev", "nodeup",
}


def _render_paragraphs(elem: ET.Element) -> list[str]:
    """Render the block-level children of ``elem`` into Markdown paragraphs."""
    paragraphs: list[str] = []
    for child in elem:
        rendered = _render_block(child)
        if rendered:
            paragraphs.append(rendered)
    return paragraphs


def _render_block(elem: ET.Element) -> str:
    tag = elem.tag
    if tag in _BLOCK_SKIP_TAGS:
        return ""
    if tag == "para":
        text = _render_inline(elem).strip()
        return text
    if tag in ("example", "smallexample", "display", "format", "lisp"):
        return _render_pre(elem)
    if tag == "verbatim":
        return _render_verbatim(elem)
    if tag in ("itemize", "enumerate"):
        return _render_list(elem, tag)
    if tag in ("table", "vtable", "ftable", "multitable"):
        return _render_table(elem)
    if tag in ("quotation", "smallquotation"):
        return _render_quotation(elem)
    if tag in ("group", "cartouche"):
        return "\n\n".join(_render_paragraphs(elem))
    # Sub-chapters/sections inside a description — flatten their content.
    if tag in ("section", "subsection", "subsubsection", "chapter", "appendix"):
        return "\n\n".join(_render_paragraphs(elem))
    # Inline-style element appearing at block level — wrap as a paragraph.
    if tag in _CODE_TAGS or tag in _EM_TAGS or tag in _STRONG_TAGS:
        return _render_inline_child(elem)
    # Unknown block — try to render children.
    return "\n\n".join(_render_paragraphs(elem))


def _render_pre(elem: ET.Element) -> str:
    pre = elem.find("pre")
    body = (pre.text if pre is not None else _plain_text(elem)) or ""
    body = body.strip("\n")
    if not body:
        return ""
    return f"```\n{body}\n```"


def _render_verbatim(elem: ET.Element) -> str:
    body = (elem.text or "").strip("\n")
    if not body:
        return ""
    return f"```\n{body}\n```"


def _render_list(elem: ET.Element, kind: str) -> str:
    items = elem.findall("listitem")
    rendered: list[str] = []
    for idx, item in enumerate(items, start=1):
        paragraphs = _render_paragraphs(item)
        if not paragraphs:
            continue
        marker = f"{idx}. " if kind == "enumerate" else "- "
        indent = " " * len(marker)
        first, *rest = paragraphs
        chunk = [marker + first.replace("\n", "\n" + indent)]
        for para in rest:
            chunk.append("")
            chunk.append(indent + para.replace("\n", "\n" + indent))
        rendered.append("\n".join(chunk))
    return "\n".join(rendered)


def _render_table(elem: ET.Element) -> str:
    rendered: list[str] = []
    for entry in elem.findall("tableentry"):
        term_parts = _table_terms(entry)
        if not term_parts:
            continue
        body_paragraphs: list[str] = []
        for item in entry.findall("tableitem"):
            body_paragraphs.extend(_render_paragraphs(item))
        term_line = " · ".join(f"**{t}**" for t in term_parts)
        if body_paragraphs:
            body = "\n\n".join(body_paragraphs)
            rendered.append(f"{term_line}  \n{body}")
        else:
            rendered.append(term_line)
    return "\n\n".join(rendered)


def _table_terms(entry: ET.Element) -> list[str]:
    terms: list[str] = []
    for term_container in entry.findall("tableterm"):
        for item in list(term_container):
            if item.tag not in ("item", "itemx"):
                continue
            text = _render_inline(item).strip()
            if text:
                terms.append(text)
    return terms


def _render_quotation(elem: ET.Element) -> str:
    paragraphs = _render_paragraphs(elem)
    if not paragraphs:
        return ""
    body = "\n\n".join(paragraphs)
    return "\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))


# === Helpers for tree traversal ====================================

_SECTION_TAGS = {
    "chapter", "section", "subsection", "subsubsection",
    "appendix", "appendixsec", "appendixsubsec", "unnumbered",
    "unnumberedsec", "unnumberedsubsec", "top",
}


def _section_title(section: ET.Element) -> str:
    title = section.find("sectiontitle")
    if title is None:
        return ""
    return _plain_text(title).strip()


def _normalize_name(value: str) -> str:
    return value.strip().lower()


# === Options =======================================================

_OPTION_NAME_RE = re.compile(r"(-{1,2}[A-Za-z0-9][A-Za-z0-9:._-]*)")

# Mapping from texi @var{type} hint (lowercased var body) to the JSON
# ``valueType`` the SPA expects. The fallback for any unrecognized but
# value-bearing hint is ``"string"`` — most ``@var`` content names a placeholder
# (e.g. ``@var{url}``, ``@var{filename}``) rather than a real type tag.
_VALUE_TYPE_BY_VAR: dict[str, str] = {
    "integer": "int",
    "int": "int",
    # ``@var{number}`` in ffmpeg.texi is almost always an integer count
    # (``-vframes number``, ``-stream_loop number``); float-typed quantities
    # use the more specific ``@var{float}`` / ``@var{double}`` hint.
    "number": "int",
    "float": "float",
    "double": "float",
    # Rational expressions carry a literal ``num/den`` form (e.g.
    # ``30000/1001``); represent them as opaque strings rather than floats
    # to avoid parseFloat() silently truncating the denominator.
    "rational": "string",
    "rational number": "string",
    "boolean": "bool",
    "bool": "bool",
    "flags": "flags",
}


def _classify_value_type(fmt: ET.Element) -> str | None:
    """Inspect ``<var>`` children of ``<itemformat>`` for a scalar type tag.

    Returns one of ``"int"`` / ``"float"`` / ``"bool"`` / ``"flags"`` / ``"string"``
    when the option carries a value, or ``None`` when no ``<var>`` is found
    (the caller treats that as the no-value case).
    """
    saw_var = False
    for var in fmt.iter("var"):
        saw_var = True
        text = (var.text or "").strip().lower()
        mapped = _VALUE_TYPE_BY_VAR.get(text)
        if mapped is not None:
            return mapped
    return "string" if saw_var else None


def _negation_siblings(
    entry: ET.Element, primary: OptionEntry
) -> list[OptionEntry]:
    """Emit a ``-no<base>`` sibling entry when the primary's description
    explicitly references one via ``@code{-noflag}`` / ``@option{-noflag}``.

    libavutil's option layer auto-generates a ``-no<flag>`` negation for every
    boolean AVOption, but the docs only call it out where it matters (e.g.
    ``-stdin`` mentions ``@code{-nostdin}``). Conservative emission — only
    surface flags the docs explicitly name — avoids inventing ``-no<flag>``
    handles that the runtime doesn't actually accept.
    """
    if primary.value_type != "none" or not primary.name.startswith("-"):
        return []
    target = f"-no{primary.name[1:]}".lower()
    for tableitem in entry.findall("tableitem"):
        for elem in tableitem.iter():
            if elem.tag not in _CODE_TAGS:
                continue
            if _plain_text(elem).strip().lower() == target:
                return [
                    OptionEntry(
                        name=target,
                        aliases=[],
                        scope=primary.scope,
                        value_type="none",
                        values=[],
                        requires=[],
                        conflicts=[],
                        description=[f"Negation form of `{primary.name}`."],
                        anchor=primary.anchor,
                        signature=[],
                    )
                ]
    return []


def _extract_enum_values(entry: ET.Element) -> list[str]:
    """Pull enum/flag values from the first ``@table @samp`` nested in the
    description.

    Items in the inner table may carry parenthetical aliases (e.g.
    ``@item none (@emph{0})``); only the leading whitespace-separated token
    survives. Empty list when no ``@samp`` table is present.
    """
    for tableitem in entry.findall("tableitem"):
        for table in tableitem.iter("table"):
            if table.get("commandarg") != "samp":
                continue
            values: list[str] = []
            for term in table.findall("tableentry/tableterm"):
                for item in term.findall("item") + term.findall("itemx"):
                    fmt = item.find("itemformat")
                    raw = _plain_text(fmt) if fmt is not None else _plain_text(item)
                    head = raw.split("(", 1)[0].strip()
                    tokens = head.split()
                    if tokens:
                        # Strip trailing punctuation — value items sometimes end
                        # in ``,`` / ``:`` / ``;`` when the docs string several
                        # synonyms in the same @item line.
                        values.append(tokens[0].rstrip(",:;."))
            if values:
                return values
    return []


def _scope_for_section(title: str, current: str) -> str:
    lower = title.lower()
    if "input" in lower:
        return "input"
    if "output" in lower:
        return "output"
    if "global" in lower or "generic" in lower:
        return "global"
    return current


def _trailing_anchor(entry: ET.Element) -> str | None:
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


def _extract_options_from_section(
    section: ET.Element,
    scope: str,
    section_anchor: str,
    sink: list[OptionEntry],
) -> None:
    for table in section.findall("table"):
        # Walk the table's entries in order, carrying forward any
        # ``@anchor{}`` makeinfo deposited at the tail of the previous entry's
        # description (see :func:`_trailing_anchor`). The first entry uses the
        # enclosing section anchor.
        pending_anchor: str | None = None
        for entry in table.findall("tableentry"):
            entry_anchor = pending_anchor or section_anchor
            option = _option_from_entry(entry, scope, entry_anchor)
            if option is not None:
                sink.append(option)
                sink.extend(_negation_siblings(entry, option))
            pending_anchor = _trailing_anchor(entry)
    # Recurse into nested subsections (e.g. "Stream specifiers" can have them).
    for sub_tag in ("section", "subsection", "subsubsection"):
        for sub in section.findall(sub_tag):
            sub_title = _section_title(sub)
            sub_scope = _scope_for_section(sub_title, scope)
            sub_anchor = _section_anchor(sub, sub_title) or section_anchor
            _extract_options_from_section(sub, sub_scope, sub_anchor, sink)


def _section_anchor(section: ET.Element, title: str) -> str:
    """Resolve a section's HTML anchor.

    An explicit ``@anchor{...}`` either inside the section (as a first child)
    or *immediately* preceding it in the source survives in makeinfo's XML as
    a sibling anchor or an in-section anchor — both are handled by the
    caller, which scans for preceding-sibling anchors when walking direct
    children. When no explicit anchor is provided, derive one from the title
    using :func:`_makeinfo_anchor`.
    """
    inner = section.find("anchor")
    if inner is not None and inner.get("name"):
        return (inner.get("name") or "").strip()
    return _makeinfo_anchor(title)


def _option_from_entry(
    entry: ET.Element, scope: str, anchor: str
) -> OptionEntry | None:
    items = entry.findall("tableterm/item") + entry.findall("tableterm/itemx")
    if not items:
        return None

    names: list[str] = []
    value_type: str = "none"
    signatures: list[str] = []
    for item in items:
        fmt = item.find("itemformat")
        # Use the full plain text of <itemformat> for name extraction so that
        # alias forms after the first ``<var>`` child (e.g.
        # ``-loglevel [<var>flags</var>+]<var>loglevel</var> | -v [<var>flags</var>+]…``)
        # are still seen by the regex. ``fmt.text`` alone stops at the first
        # element child and drops every name after it.
        head = _plain_text(fmt) if fmt is not None else (item.text or "")
        # Strip the trailing role/scope parenthetical (e.g.
        # ``(input/output,per-stream)`` or ``(@code{-V})``) before scanning for
        # option names — the regex would otherwise see ``-stream`` inside
        # ``per-stream`` or ``-V`` inside the parenthetical and emit them as
        # spurious aliases. Names always appear before the first ``(``.
        head_for_names = head.split("(", 1)[0] if head else head
        for name in _OPTION_NAME_RE.findall(head_for_names or ""):
            names.append(_normalize_name(name))
        if fmt is not None:
            classified = _classify_value_type(fmt)
            if classified is not None and value_type == "none":
                value_type = classified
            elif "=" in (fmt.text or "") and value_type == "none":
                # Items like ``-define key=value`` carry a value via the
                # ``=`` literal even when no ``@var`` is present.
                value_type = "string"
            # Render the documented signature with @var/@emph stripped — the
            # raw text preserves the bracket grammar (``[-]input_file_id``,
            # ``[:stream_specifier]``) which the description prose references
            # by name. Collapse internal whitespace so makeinfo's line wraps
            # don't survive into the JSON.
            sig = " ".join(_plain_text(fmt).split())
            if sig:
                signatures.append(sig)

    if not names:
        return None

    description_paragraphs: list[str] = []
    for item in entry.findall("tableitem"):
        description_paragraphs.extend(_render_paragraphs(item))

    # Promote to ``enum`` when the description carries an ``@table @samp``
    # value list. ``flags``-typed options keep their type tag (they accept
    # ``+a+b`` combinations rather than a single enum match) but still
    # surface the documented value set.
    values = _extract_enum_values(entry)
    if values and value_type not in ("flags",):
        value_type = "enum"

    return OptionEntry(
        name=names[0],
        aliases=names[1:],
        scope=scope,
        value_type=value_type,
        values=values,
        requires=[],
        conflicts=[],
        description=description_paragraphs,
        anchor=anchor,
        signature=signatures,
    )


def parse_options_xml(root: ET.Element) -> list[OptionEntry]:
    options: list[OptionEntry] = []

    def visit(elem: ET.Element, scope: str, current_anchor: str) -> None:
        # Track the most recent preceding-sibling <anchor name="..."> so that
        # ``@anchor{Stream selection}\\n@chapter Stream selection`` gives the
        # chapter the anchor's name. (Makeinfo emits the anchor as a sibling
        # of the chapter, not a child.)
        pending_anchor: str | None = None
        for child in elem:
            if child.tag == "anchor":
                name = (child.get("name") or "").strip()
                if name:
                    pending_anchor = name
                continue
            if child.tag in _SECTION_TAGS:
                title = _section_title(child)
                child_scope = _scope_for_section(title, scope)
                child_anchor = (
                    pending_anchor or _section_anchor(child, title) or current_anchor
                )
                _extract_options_from_section(child, child_scope, child_anchor, options)
                visit(child, child_scope, child_anchor)
                pending_anchor = None
            else:
                visit(child, scope, current_anchor)
                pending_anchor = None

    visit(root, "global", "")
    return options


# === AVCodec / AVFormat options ====================================

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
        text = _plain_text(emph).strip().lower()
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

    Reuses the value-type and enum helpers from the driver-option path.
    Names come without a leading ``-`` in the docs (``@item b @var{integer}``);
    the leading dash is added on emit so the SPA can resolve the option from
    the command-line form (``-b``).
    """
    items = entry.findall("tableterm/item") + entry.findall("tableterm/itemx")
    if not items:
        return None

    names: list[str] = []
    value_type = "none"
    signatures: list[str] = []
    roles: list[str] = []

    for item in items:
        fmt = item.find("itemformat")
        if fmt is None:
            continue
        head = _plain_text(fmt)
        head_for_name = head.split("(", 1)[0].strip()
        match = _AV_OPTION_NAME_RE.match(head_for_name)
        if match:
            names.append(f"-{_normalize_name(match.group(1))}")

        classified = _classify_value_type(fmt)
        if classified is not None and value_type == "none":
            value_type = classified

        sig = " ".join(head.split())
        if sig:
            signatures.append(sig)

        for role in _av_option_roles(fmt, allowed_roles):
            if role not in roles:
                roles.append(role)

    if not names:
        return None

    description_paragraphs: list[str] = []
    for item in entry.findall("tableitem"):
        description_paragraphs.extend(_render_paragraphs(item))

    values = _extract_enum_values(entry)
    if values and value_type not in ("flags",):
        value_type = "enum"

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
        chapter_anchor = _section_anchor(chapter, _section_title(chapter))

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
            inner_pending = _trailing_anchor(entry)

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


# === Per-codec private options =====================================
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
    items = entry.findall("tableterm/item") + entry.findall("tableterm/itemx")
    if not items:
        return None

    names: list[str] = []
    value_type = "string"  # encoders.texi default — most items take values.
    signatures: list[str] = []

    for item in items:
        fmt = item.find("itemformat")
        if fmt is None:
            continue
        head = _plain_text(fmt)
        head_for_name = head.split("(", 1)[0].strip()
        # ac3 / a few others write ``@item -per_frame_metadata @var{boolean}``;
        # strip the leading dash before matching so the regex (which expects
        # an alpha lead char) succeeds either way.
        name_text = head_for_name.lstrip("-")
        match = _AV_OPTION_NAME_RE.match(name_text)
        if match:
            names.append(f"-{_normalize_name(match.group(1))}")

        classified = _classify_value_type(fmt)
        if classified is not None and classified != "string":
            value_type = classified

        sig = " ".join(head.split())
        if sig:
            signatures.append(sig)

    if not names:
        return None

    description_paragraphs: list[str] = []
    for item in entry.findall("tableitem"):
        description_paragraphs.extend(_render_paragraphs(item))

    values = _extract_enum_values(entry)
    if values and value_type not in ("flags",):
        value_type = "enum"

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
    section_anchor = _section_anchor(section, _section_title(section))
    out: list[AVOptionEntry] = []
    for table in section.iter("table"):
        if table.get("commandarg") != "option":
            continue
        for entry in table.findall("tableentry"):
            option = _per_codec_option_from_entry(entry, section_anchor, side)
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
            if child.tag not in _SECTION_TAGS:
                continue
            title = _section_title(child)

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

            aliases = _codec_aliases_from_title(title)
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
        if child.tag in _SECTION_TAGS:
            break
        if child.tag != "table" or child.get("commandarg") != "samp":
            continue
        for term in child.findall("tableentry/tableterm"):
            for item in term.findall("item") + term.findall("itemx"):
                fmt = item.find("itemformat")
                raw = _plain_text(fmt) if fmt is not None else _plain_text(item)
                head = raw.split("(", 1)[0].strip()
                tokens = head.split()
                if not tokens:
                    continue
                token = _normalize_name(tokens[0].rstrip(",:;."))
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
            if child.tag not in _SECTION_TAGS:
                continue
            title = _section_title(child)

            if child.tag == "chapter":
                lower = title.lower()
                if "muxer" in lower or "demuxer" in lower:
                    visit(child, True)
                else:
                    visit(child, False)
                continue

            if not in_chapter or child.tag != "section":
                continue

            aliases = _codec_aliases_from_title(title)
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
                merged[name][idx] = AVOptionEntry(
                    name=prev.name,
                    aliases=prev.aliases,
                    value_type=prev.value_type,
                    values=prev.values,
                    description=prev.description,
                    anchor=prev.anchor,
                    signature=prev.signature,
                    roles=combined_roles,
                    value_descriptions=prev.value_descriptions,
                )
            else:
                merged[name].append(option)
    return merged


# === Codecs ========================================================

# Headings that switch the current codec type when walking codecs.texi.
def _codec_type_for_heading(title: str) -> str | None:
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


def _codec_aliases_from_title(title: str) -> list[str]:
    parts = [p.strip() for p in _CODEC_TITLE_SPLIT.split(title) if p.strip()]
    out: list[str] = []
    for part in parts:
        # Drop trailing parens/qualifiers — e.g. "rawvideo (raw video)" → "rawvideo".
        token = part.split("(", 1)[0].strip().split()[0] if part.split() else ""
        normalized = _normalize_name(token)
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
            if child.tag not in _SECTION_TAGS:
                visit(child, type_, role, in_codec_chapter)
                pending_anchor = None
                continue

            title = _section_title(child)
            new_type = _codec_type_for_heading(title) or type_
            new_role = _codec_role_for_heading(title) or role
            section_anchor = pending_anchor or _section_anchor(child, title)
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
                aliases = _codec_aliases_from_title(title)
                if aliases:
                    codecs.append(
                        CodecEntry(
                            name=aliases[0],
                            type=new_type,
                            aliases=aliases[1:],
                            encoder=new_role[0],
                            decoder=new_role[1],
                            anchor=section_anchor,
                        )
                    )
                is_codec_section = True

            # Descend regardless — sections can have meaningful sub-sections.
            visit(child, new_type, new_role, in_codec_chapter and not is_codec_section)

    visit(root, "video", (False, False), False)
    return codecs


# === Filters =======================================================

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
        normalized = _normalize_name(token)
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
                raw = _plain_text(fmt) if fmt is not None else _plain_text(item)
                arg_name = _arg_name_from_text(raw)
                if not arg_name:
                    continue
                if arg_name not in args:
                    args[arg_name] = []
                    params.append(arg_name)
                body: list[str] = []
                for ti in entry.findall("tableitem"):
                    body.extend(_render_paragraphs(ti))
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
    return _normalize_name(head)


def _filter_description(section: ET.Element) -> list[str]:
    """Render all block content of ``section`` except the args @table."""
    paragraphs: list[str] = []
    # Skip the first @table @option (it's the args table, already extracted).
    skipped_args_table = False
    for child in section:
        if child.tag == "table" and not skipped_args_table:
            skipped_args_table = True
            continue
        if child.tag in _SECTION_TAGS:
            # Sub-sections like "Examples"/"Commands" — flatten into desc.
            sub = _render_paragraphs(child)
            if sub:
                title = _section_title(child).strip()
                if title:
                    paragraphs.append(f"**{title}**")
                paragraphs.extend(sub)
            continue
        rendered = _render_block(child)
        if rendered:
            paragraphs.append(rendered)
    return paragraphs


def parse_filters_xml(root: ET.Element) -> list[FilterEntry]:
    filters: list[FilterEntry] = []

    def visit(elem: ET.Element, type_: str, in_filter_chapter: bool) -> None:
        for child in elem:
            if child.tag not in _SECTION_TAGS:
                visit(child, type_, in_filter_chapter)
                continue

            title = _section_title(child)
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


# === Codec list from libavcodec C source ===========================

def parse_codecs_c(text: str) -> dict[str, dict[str, bool]]:
    entries: dict[str, dict[str, bool]] = {}
    for match in re.finditer(r"ff_([a-z0-9_]+)_(encoder|decoder)", text):
        name = _normalize_name(match.group(1))
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


# === Dedupe helpers (kept stable for the SPA) ======================

def dedupe_options(options: list[OptionEntry]) -> list[OptionEntry]:
    seen: dict[str, OptionEntry] = {}
    claimed_aliases: set[str] = set()
    for option in options:
        if option.name in seen or option.name in claimed_aliases:
            # A later, weaker entry sharing a name with an existing primary
            # (e.g. ``-v`` appearing both as alias of ``-loglevel`` and as a
            # stub item somewhere else) is dropped to keep aliases unique.
            continue
        seen[option.name] = option
        for alias in option.aliases:
            claimed_aliases.add(alias)
    return sorted(seen.values(), key=lambda o: o.name)


def dedupe_av_options(options: list[AVOptionEntry]) -> list[AVOptionEntry]:
    seen: dict[str, AVOptionEntry] = {}
    claimed_aliases: set[str] = set()
    for option in options:
        if option.name in seen or option.name in claimed_aliases:
            continue
        seen[option.name] = option
        for alias in option.aliases:
            claimed_aliases.add(alias)
    return sorted(seen.values(), key=lambda o: o.name)


def dedupe_codecs(codecs: list[CodecEntry]) -> list[CodecEntry]:
    seen: dict[str, CodecEntry] = {}
    for codec in codecs:
        if codec.name not in seen:
            seen[codec.name] = codec
    return sorted(seen.values(), key=lambda c: c.name)


def dedupe_filters(filters: list[FilterEntry]) -> list[FilterEntry]:
    seen: dict[str, FilterEntry] = {}
    for flt in filters:
        if flt.name not in seen:
            seen[flt.name] = flt
    return sorted(seen.values(), key=lambda f: f.name)


def dedupe_named(entries: list[NamedEntry]) -> list[NamedEntry]:
    seen: dict[str, NamedEntry] = {}
    for entry in entries:
        if entry.name not in seen:
            seen[entry.name] = entry
    return sorted(seen.values(), key=lambda e: e.name)


# === Named-section catalogs (demuxers, muxers, protocols, bitstream filters) ==

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
        normalized = _normalize_name(token)
        if normalized:
            out.append(normalized)
    return out


def _makeinfo_anchor(title: str) -> str:
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


def _anchor_for_section(section_anchor: str | None, fallback_title: str) -> str:
    """The HTML anchor that links to this entity.

    Explicit ``@anchor{...}`` wins — its value already comes through the XML
    in the encoded form makeinfo will use in the HTML output (e.g.
    ``@anchor{raw muxers}`` ⇒ ``raw-muxers``). Otherwise we derive the
    auto-anchor from the section title using the same encoding makeinfo
    applies when it generates the HTML id.
    """
    if section_anchor:
        return section_anchor
    return _makeinfo_anchor(fallback_title)


def _named_description(section: ET.Element) -> list[str]:
    """Render a section's block content as Markdown paragraphs.

    Subsections (Examples, Options, Syntax, Background, …) are flattened in,
    matching the filter parser's treatment.
    """
    paragraphs: list[str] = []
    for child in section:
        if child.tag in _SECTION_TAGS:
            sub = _render_paragraphs(child)
            if sub:
                title = _section_title(child).strip()
                if title:
                    paragraphs.append(f"**{title}**")
                paragraphs.extend(sub)
            continue
        rendered = _render_block(child)
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
                    _plain_text(fmt) if fmt is not None else _plain_text(item)
                ).strip()
                if not raw:
                    continue
                # Pull a single token from the item head; ignore any trailing
                # @emph{audio}/@emph{video} marker and parenthetical aliases —
                # but capture parenthetical aliases as additional names.
                head = raw.split()[0].strip(",")
                normalized = _normalize_name(head)
                if normalized:
                    names.append(normalized)
                paren_match = re.search(r"\(([^)]+)\)", raw)
                if paren_match:
                    for piece in paren_match.group(1).split(","):
                        alias = _normalize_name(piece.strip())
                        if alias:
                            aliases.append(alias)
            if not names:
                continue
            description: list[str] = []
            for body in entry.findall("tableitem"):
                description.extend(_render_paragraphs(body))
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
        title = _section_title(chapter)
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

            section_title = _section_title(child).strip()
            inner_anchor = child.find("anchor")
            anchor_value = pending_anchor
            if inner_anchor is not None and inner_anchor.get("name"):
                anchor_value = (inner_anchor.get("name") or "").strip()
            pending_anchor = None

            if _is_grouped_section_title(section_title):
                # The group section itself doesn't represent a single entity.
                # Use its anchor (or section title) as the fallback link for
                # each @samp item it contains.
                fallback = _anchor_for_section(anchor_value, section_title)
                sink.extend(_named_entries_from_samp_table(child, fallback))
                continue

            aliases = _named_aliases_from_title(section_title)
            if not aliases:
                continue
            primary = aliases[0]
            sink.append(
                NamedEntry(
                    name=primary,
                    aliases=aliases[1:],
                    anchor=_anchor_for_section(anchor_value, section_title),
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
