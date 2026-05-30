"""Markdown rendering of the XML emitted by ``makeinfo --xml``.

Walks an ElementTree element and turns its mixed inline/block content
into a list of Markdown paragraph strings — the same shape the SPA
consumes. Self-contained: no project imports beyond ``ElementTree``.

The public surface is :func:`plain_text`, :func:`render_inline`,
:func:`render_paragraphs`, :func:`render_block`, plus the
:data:`CODE_TAGS` / :data:`EM_TAGS` / :data:`STRONG_TAGS` sets a few
option-parser helpers consult to recognize inline-tagged option mentions
inside a description.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

_MD_SPECIALS = r"\`*_[]"


def _md_escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _MD_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


# Inline tags that render as a Markdown code span.
CODE_TAGS = {
    "code", "command", "option", "samp", "file", "kbd", "key",
    "verb", "env", "t", "indicateurl",
}
# Inline tags that render as Markdown emphasis (italic).
EM_TAGS = {"var", "emph", "i", "cite", "dfn"}
# Inline tags that render as Markdown strong (bold).
STRONG_TAGS = {"strong", "b"}
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


def plain_text(elem: ET.Element) -> str:
    """Recursively collect the text content of ``elem`` (no Markdown markup)."""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.tag in _DROP_INLINE_TAGS:
            pass
        else:
            parts.append(plain_text(child))
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


def render_inline(elem: ET.Element) -> str:
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
    if tag in CODE_TAGS:
        return _wrap_code(plain_text(elem))
    if tag in EM_TAGS:
        inner = render_inline(elem).strip()
        return f"*{inner}*" if inner else ""
    if tag in STRONG_TAGS:
        inner = render_inline(elem).strip()
        return f"**{inner}**" if inner else ""
    if tag in _PASSTHROUGH_TAGS:
        return render_inline(elem)
    if tag in ("uref", "url"):
        return _render_uref(elem)
    if tag == "email":
        return _render_email(elem)
    if tag in ("ref", "xref", "pxref", "inforef"):
        return _render_xref(elem, tag)
    if tag == "linebreak":
        return "\n"
    # Unknown element — recurse so we don't drop meaningful text.
    return render_inline(elem)


def _child_text(elem: ET.Element | None, child_tag: str) -> str:
    if elem is None:
        return ""
    child = elem.find(child_tag)
    if child is None:
        return ""
    return plain_text(child).strip()


def _render_uref(elem: ET.Element) -> str:
    url = _child_text(elem, "urefurl")
    desc = _child_text(elem, "urefdesc") or _child_text(elem, "urefreplacement")
    if not url:
        # @url{some text} without a real URL — just return the text.
        return _md_escape(plain_text(elem).strip())
    if not desc or desc == url:
        return f"<{url}>"
    return f"[{_md_escape(desc)}]({url})"


def _render_email(elem: ET.Element) -> str:
    addr = _child_text(elem, "emailaddress")
    name = _child_text(elem, "emailname")
    if not addr:
        return _md_escape(plain_text(elem).strip())
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


def render_paragraphs(elem: ET.Element) -> list[str]:
    """Render the block-level children of ``elem`` into Markdown paragraphs."""
    paragraphs: list[str] = []
    for child in elem:
        rendered = render_block(child)
        if rendered:
            paragraphs.append(rendered)
    return paragraphs


def render_block(elem: ET.Element) -> str:
    tag = elem.tag
    if tag in _BLOCK_SKIP_TAGS:
        return ""
    if tag == "para":
        text = render_inline(elem).strip()
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
        return "\n\n".join(render_paragraphs(elem))
    # Sub-chapters/sections inside a description — flatten their content.
    if tag in ("section", "subsection", "subsubsection", "chapter", "appendix"):
        return "\n\n".join(render_paragraphs(elem))
    # Inline-style element appearing at block level — wrap as a paragraph.
    if tag in CODE_TAGS or tag in EM_TAGS or tag in STRONG_TAGS:
        return _render_inline_child(elem)
    # Unknown block — try to render children.
    return "\n\n".join(render_paragraphs(elem))


def _render_pre(elem: ET.Element) -> str:
    pre = elem.find("pre")
    body = (pre.text if pre is not None else plain_text(elem)) or ""
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
        paragraphs = render_paragraphs(item)
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
            body_paragraphs.extend(render_paragraphs(item))
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
            text = render_inline(item).strip()
            if text:
                terms.append(text)
    return terms


def _render_quotation(elem: ET.Element) -> str:
    paragraphs = render_paragraphs(elem)
    if not paragraphs:
        return ""
    body = "\n\n".join(paragraphs)
    return "\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))
