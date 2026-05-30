"""Driver-level FFmpeg options (the things you pass on the CLI).

Walks the ``@table @option`` blocks inside ``ffmpeg.texi``'s sections,
classifying each as global/input/output by section title, and emits one
:class:`OptionEntry` per ``@item``/``@itemx`` row. The value-type and
enum-value helpers (:func:`classify_value_type`, :func:`extract_enum_values`)
are public to the package because the AVOption variants reuse the exact
same shape recognition.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .models import OptionEntry
from .texi_markdown import CODE_TAGS, plain_text, render_paragraphs
from .texi_traversal import (
    SECTION_TAGS,
    normalize_name,
    section_anchor,
    section_title,
    trailing_anchor,
)

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


def classify_value_type(fmt: ET.Element) -> str | None:
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
            if elem.tag not in CODE_TAGS:
                continue
            if plain_text(elem).strip().lower() == target:
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


def extract_enum_values(entry: ET.Element) -> list[str]:
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
                    raw = plain_text(fmt) if fmt is not None else plain_text(item)
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


def _extract_options_from_section(
    section: ET.Element,
    scope: str,
    section_anchor_value: str,
    sink: list[OptionEntry],
) -> None:
    for table in section.findall("table"):
        # Walk the table's entries in order, carrying forward any
        # ``@anchor{}`` makeinfo deposited at the tail of the previous entry's
        # description (see :func:`trailing_anchor`). The first entry uses the
        # enclosing section anchor.
        pending_anchor: str | None = None
        for entry in table.findall("tableentry"):
            entry_anchor = pending_anchor or section_anchor_value
            option = _option_from_entry(entry, scope, entry_anchor)
            if option is not None:
                sink.append(option)
                sink.extend(_negation_siblings(entry, option))
            pending_anchor = trailing_anchor(entry)
    # Recurse into nested subsections (e.g. "Stream specifiers" can have them).
    for sub_tag in ("section", "subsection", "subsubsection"):
        for sub in section.findall(sub_tag):
            sub_title = section_title(sub)
            sub_scope = _scope_for_section(sub_title, scope)
            sub_anchor = section_anchor(sub, sub_title) or section_anchor_value
            _extract_options_from_section(sub, sub_scope, sub_anchor, sink)


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
        head = plain_text(fmt) if fmt is not None else (item.text or "")
        # Strip the trailing role/scope parenthetical (e.g.
        # ``(input/output,per-stream)`` or ``(@code{-V})``) before scanning for
        # option names — the regex would otherwise see ``-stream`` inside
        # ``per-stream`` or ``-V`` inside the parenthetical and emit them as
        # spurious aliases. Names always appear before the first ``(``.
        head_for_names = head.split("(", 1)[0] if head else head
        for name in _OPTION_NAME_RE.findall(head_for_names or ""):
            names.append(normalize_name(name))
        if fmt is not None:
            classified = classify_value_type(fmt)
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
            sig = " ".join(plain_text(fmt).split())
            if sig:
                signatures.append(sig)

    if not names:
        return None

    description_paragraphs: list[str] = []
    for item in entry.findall("tableitem"):
        description_paragraphs.extend(render_paragraphs(item))

    # Promote to ``enum`` when the description carries an ``@table @samp``
    # value list. ``flags``-typed options keep their type tag (they accept
    # ``+a+b`` combinations rather than a single enum match) but still
    # surface the documented value set.
    values = extract_enum_values(entry)
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
            if child.tag in SECTION_TAGS:
                title = section_title(child)
                child_scope = _scope_for_section(title, scope)
                child_anchor = (
                    pending_anchor or section_anchor(child, title) or current_anchor
                )
                _extract_options_from_section(child, child_scope, child_anchor, options)
                visit(child, child_scope, child_anchor)
                pending_anchor = None
            else:
                visit(child, scope, current_anchor)
                pending_anchor = None

    visit(root, "global", "")
    return options


__all__ = [
    "classify_value_type",
    "extract_enum_values",
    "parse_options_xml",
]
