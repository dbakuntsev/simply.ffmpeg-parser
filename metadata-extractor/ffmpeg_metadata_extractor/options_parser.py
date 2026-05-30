"""Driver-level FFmpeg options (the things you pass on the CLI).

Walks the ``@table @option`` blocks inside ``ffmpeg.texi``'s sections,
classifying each as global/input/output by section title, and emits one
:class:`OptionEntry` per ``@item``/``@itemx`` row. The value-type and
enum-value helpers (:func:`classify_value_type`, :func:`extract_enum_values`)
are public to the package because the AVOption variants reuse the exact
same shape recognition.

This module also exposes :func:`iter_item_heads` and
:func:`render_entry_body` — the two pieces of scaffolding the three
"entry → OptionEntry" builders share. Variants differ in how they
extract names from the head text, how they default and update
``value_type``, and (for AVCodec) whether they read role tags; everything
else is shared.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
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


# --- Shared "entry → typed option" scaffolding ------------------------
#
# All three entry builders (driver options, generic AVOptions, per-codec
# private AVOptions) share the same outer shape:
#
#   1. Iterate <tableterm>/<item|itemx> rows; bail if there are none.
#   2. Per row: extract names from the head text, contribute to
#      ``value_type``, collect the documented signature, (optionally) read
#      role tags from <emph> children.
#   3. Bail if no names were collected.
#   4. Render the description paragraphs from <tableitem> children.
#   5. Pull enum/flag values from a nested ``@table @samp`` and promote
#      ``value_type`` to "enum" when applicable.
#
# Steps 1, 4, 5 are identical across variants; only step 2 differs
# (regex flavor, default ``value_type``, override policy, role handling).
# :func:`iter_item_heads` and :func:`render_entry_body` capture steps 1
# and 4+5; each variant supplies its own step-2 loop body.


@dataclass(frozen=True)
class ItemHead:
    """Per-item view used by every entry-builder variant.

    ``fmt`` is the ``<itemformat>`` child (may be ``None`` when the
    ``<item>`` is bare text — driver options accept that case, the
    AVOption variants skip such rows). ``head_for_names`` is the text
    before the first ``(`` — option names always appear there, so this
    pre-strips the scope/role parenthetical (e.g. ``(input/output,per-stream)``
    or ``(@code{-V})``) that would otherwise pollute name extraction.
    ``signature`` is the whitespace-collapsed full head, used verbatim as
    the documented invocation form.
    """

    fmt: ET.Element | None
    head_text: str
    head_for_names: str
    signature: str


def iter_item_heads(entry: ET.Element) -> Iterator[ItemHead]:
    """Yield one :class:`ItemHead` per ``<tableterm>``/``<item|itemx>``
    in ``entry``.

    Callers iterate this once and supply their own variant-specific name
    extraction + value-type policy on each yielded head. The text
    extraction, parenthetical strip, and signature collapse are uniform
    across variants and live here.
    """
    for item in entry.findall("tableterm/item") + entry.findall("tableterm/itemx"):
        fmt = item.find("itemformat")
        if fmt is not None:
            head_text = plain_text(fmt)
            signature = " ".join(head_text.split())
        else:
            head_text = item.text or ""
            signature = ""
        head_for_names = head_text.split("(", 1)[0].strip()
        yield ItemHead(
            fmt=fmt,
            head_text=head_text,
            head_for_names=head_for_names,
            signature=signature,
        )


def render_entry_body(
    entry: ET.Element, value_type: str
) -> tuple[list[str], list[str], str]:
    """Render the entry's description paragraphs and enum value list.

    Returns ``(description_paragraphs, values, final_value_type)``. The
    returned ``final_value_type`` is the input ``value_type`` promoted to
    ``"enum"`` when a value list is present and the current type isn't
    ``"flags"`` (which keeps its tag because ``flags`` accepts ``+a+b``
    combinations rather than a single enum match — but still surfaces the
    documented value set).
    """
    description_paragraphs: list[str] = []
    for item in entry.findall("tableitem"):
        description_paragraphs.extend(render_paragraphs(item))
    values = extract_enum_values(entry)
    if values and value_type != "flags":
        value_type = "enum"
    return description_paragraphs, values, value_type


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
    heads = list(iter_item_heads(entry))
    if not heads:
        return None

    names: list[str] = []
    value_type: str = "none"
    signatures: list[str] = []
    for head in heads:
        # Use the full plain text of <itemformat> (head.head_for_names) for
        # name extraction so that alias forms after the first ``<var>`` child
        # (e.g. ``-loglevel [<var>flags</var>+]<var>loglevel</var> | -v
        # [<var>flags</var>+]…``) are still seen by the regex.
        for name in _OPTION_NAME_RE.findall(head.head_for_names):
            names.append(normalize_name(name))
        if head.fmt is not None:
            classified = classify_value_type(head.fmt)
            if classified is not None and value_type == "none":
                value_type = classified
            elif "=" in (head.fmt.text or "") and value_type == "none":
                # Items like ``-define key=value`` carry a value via the
                # ``=`` literal even when no ``@var`` is present.
                value_type = "string"
            if head.signature:
                signatures.append(head.signature)

    if not names:
        return None

    description_paragraphs, values, value_type = render_entry_body(entry, value_type)

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
    "ItemHead",
    "classify_value_type",
    "extract_enum_values",
    "iter_item_heads",
    "parse_options_xml",
    "render_entry_body",
]
