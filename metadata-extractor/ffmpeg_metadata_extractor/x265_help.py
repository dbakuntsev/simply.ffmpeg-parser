"""Parse upstream x265 source to extract the value lists + descriptions
for libx265's ``-preset``, ``-tune``, and ``-profile`` (which FFmpeg
declares as ``AV_OPT_TYPE_STRING`` passthroughs into x265's
``x265_param_default_preset`` / ``x265_param_apply_profile`` API).

Unlike x264 (whose CLI emits a richly-formatted ``--fullhelp`` table
this module's sibling :mod:`.x264_help` parses), x265's CLI just prints
the value names in one line and provides no per-value description in the
help text. So the value names are mined here from the IMPLEMENTATIONS
themselves:

- ``source/common/param.cpp`` — ``x265_param_default_preset()`` contains
  an ``if (!strcmp(preset, "<name>")) { param->field = value; ... }``
  cascade for both presets and tunes. The field assignments inside each
  branch ARE the meaningful description of what the preset/tune sets.

- ``source/encoder/level.cpp`` — ``x265_param_apply_profile()`` contains
  scattered ``strcmp(profile, "<name>")`` checks across bit-depth and
  chroma-format guards. There's no single per-profile body to extract,
  so this parser collects the unique profile names + a synthesized
  description derived from the chroma-format branch each name appears in.
"""

from __future__ import annotations

import re
from pathlib import Path

from .x264_help import UpstreamOptionHelp


# ``if (!strcmp(NAME_VAR, "VALUE"))`` — ``NAME_VAR`` is ``preset``,
# ``tune``, or ``profile`` depending on which function we're inside.
_STRCMP_BRANCH = re.compile(r'!\s*strcmp\s*\(\s*(\w+)\s*,\s*"([^"\\]+)"\s*\)')

# ``param->some_field`` / ``param->rc.cuTree`` etc. — RHS captured up to
# the trailing semicolon. We don't try to interpret the RHS, just present
# it verbatim. Multi-line continuations are folded by replacing internal
# whitespace with a single space.
_ASSIGNMENT = re.compile(
    r"param\s*->\s*([A-Za-z_][\w.]*)\s*=\s*([^;]+?)\s*;",
    re.DOTALL,
)

# ``int FUNCNAME(...)`` followed by ``{``. The name varies, so we accept
# any return type token.
_FUNC_OPEN = r"\b{name}\s*\([^)]*\)\s*\n*\{{"


def _strip_comments(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments. C-style strings inside the
    function bodies we parse don't contain ``//`` or ``/*``, so we don't
    need a full lexer here."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _find_function_body(text: str, name: str) -> str:
    """Return the body of ``<retval> name(...)`` from opening ``{`` to
    its matching ``}``, or ``""`` if not found. Walks brace depth, so
    nested ``if`` / ``for`` blocks don't confuse the boundary.
    """
    m = re.search(_FUNC_OPEN.format(name=re.escape(name)), text)
    if not m:
        return ""
    open_idx = m.end() - 1  # the ``{``
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            # Skip string literal — they CAN contain ``{`` / ``}``.
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
        i += 1
    return ""


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Given ``text[open_idx] == '{'``, return the matching ``}``'s index
    or ``-1``. String/char literals don't perturb the brace count."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _format_assignments(body: str) -> str:
    """Pull every ``param->FIELD = VALUE;`` out of ``body`` and return
    one ``FIELD = VALUE`` per line. Whitespace inside RHS is folded so
    multi-line ternaries collapse to a single readable line."""
    lines: list[str] = []
    for m in _ASSIGNMENT.finditer(body):
        field = m.group(1).strip()
        value = re.sub(r"\s+", " ", m.group(2).strip())
        lines.append(f"{field} = {value}")
    return "\n".join(lines)


def _extract_strcmp_cascade(
    body: str, var_name: str
) -> list[tuple[str, str]]:
    """Walk a body containing ``if/else if (!strcmp(var_name, "X")) { ... }``
    branches and return ``[(name, formatted_body), ...]`` in source order.

    Branches that share a body (``if (!strcmp(t, "foo") || !strcmp(t, "bar"))``)
    surface each name with the same description. Empty bodies surface as
    empty descriptions — e.g. x265's ``vmaf`` tune currently has no
    assignments, which is information in its own right.
    """
    entries: list[tuple[str, str]] = []
    seen_branches: set[int] = set()  # body-opening-brace positions
    n = len(body)
    for m in _STRCMP_BRANCH.finditer(body):
        if m.group(1) != var_name:
            continue
        name = m.group(2)
        # Walk forward from the strcmp() match to the next ``{`` at depth 0
        # — that's the branch body. If the next non-whitespace is NOT ``{``
        # the branch may be a one-liner (rare in this codebase); we'd
        # capture up to the semicolon. For x265, every branch uses braces.
        i = m.end()
        while i < n and body[i] not in "{;":
            i += 1
        if i >= n or body[i] != "{":
            entries.append((name, ""))
            continue
        # Multiple strcmp() in the same `if (a || b)` share one body.
        # De-dup by body position so we emit each branch once per name
        # but each name gets the body's full description.
        end = _find_matching_brace(body, i)
        if end == -1:
            continue
        formatted = _format_assignments(body[i + 1 : end])
        entries.append((name, formatted))
        seen_branches.add(i)
    return entries


def _extract_profile_names(body: str) -> list[tuple[str, str]]:
    """Profile validation in x265 is a scatter of ``strcmp(profile, "X")``
    calls across bit-depth and chroma-format branches. There's no single
    branch body per profile, so we just collect unique names in source
    order. Each name's description is left empty — the value list itself
    is the useful information.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _STRCMP_BRANCH.finditer(body):
        if m.group(1) != "profile":
            continue
        name = m.group(2)
        if name in seen:
            continue
        seen.add(name)
        out.append((name, ""))
    return out


def parse_x265_help(
    param_cpp: str, level_cpp: str
) -> dict[str, UpstreamOptionHelp]:
    """Return ``{option: UpstreamOptionHelp}`` for preset / tune / profile.

    Returns the same schema as :func:`.x264_help.parse_x264_help` so the
    extractor's layering helper can consume both uniformly. x265's CLI
    help carries no meaningful per-option header description (just bare
    value-name lists), so ``description`` stays empty here — only the
    ``values`` field is populated.

    ``param_cpp``: contents of ``source/common/param.cpp`` — supplies
    presets + tunes.
    ``level_cpp``: contents of ``source/encoder/level.cpp`` — supplies
    profile names.

    Either argument may be empty; the corresponding entries simply don't
    appear in the result.
    """
    out: dict[str, UpstreamOptionHelp] = {}

    if param_cpp:
        clean = _strip_comments(param_cpp)
        body = _find_function_body(clean, "x265_param_default_preset")
        if body:
            presets = _extract_strcmp_cascade(body, "preset")
            if presets:
                out["preset"] = UpstreamOptionHelp(values=presets)
            tunes = _extract_strcmp_cascade(body, "tune")
            if tunes:
                out["tune"] = UpstreamOptionHelp(values=tunes)

    if level_cpp:
        clean = _strip_comments(level_cpp)
        body = _find_function_body(clean, "x265_param_apply_profile")
        if body:
            profiles = _extract_profile_names(body)
            if profiles:
                out["profile"] = UpstreamOptionHelp(values=profiles)

    return out
