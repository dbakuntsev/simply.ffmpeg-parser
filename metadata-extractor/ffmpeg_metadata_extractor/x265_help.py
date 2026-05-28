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

from .upstream_help import HelpDoc, HelpSection, UpstreamOptionHelp
from .x264_help import (
    _H_CALL,
    _STRING_LITERAL,
    _decode_escapes,
    _find_matching_paren,
    _parse_constants,
    _parse_defaults,
    _parse_string_tables,
    _resolve_format,
    _split_top_level_commas,
)

# x265 ``showHelp(x265_param* param)`` — locate the opening brace.
_X265_SHOWHELP_SIG = r"\bshowHelp\s*\(\s*x265_param\s*\*\s*\w+\s*\)\s*\n*\{"
# x265's defaults function (same flat ``param->X = …;`` shape as x264).
_X265_PARAM_DEFAULT_SIG = (
    r"\bx265_param_default\s*\(\s*x265_param\s*\*\s*\w+\s*\)\s*\n*\{"
)

# Section header in the flattened help: capitalized text + colon at
# column 0. x265 titles carry commas and slashes ("Profile, Level,
# Tier:", "Temporal / motion search options:"), so the character class
# is broader than x264's.
_X265_SECTION = re.compile(r"^([A-Z][A-Za-z][A-Za-z ,/\-]*):\s*$")

# x265 option header. Handles the short-slash prefix (``-D/--``), the
# ``--[no-]`` boolean-toggle form, and an optional value spec that may
# be ``<...>`` OR a bare token like ``8|10|12`` / ``WxH`` / ``bff|tff``.
# Captures the long option name (group 1) and the trailing description
# (group 2). Bounded leading indent keeps continuation lines (col 33+)
# from being misread as headers.
_X265_OPTION_HEADER = re.compile(
    r"^[ \t]{0,4}(?:-\w/)?--(?:\[no-?\])?([A-Za-z][\w-]*)"
    r"(?:\s+(?:<[^>]*>|[\w|]+x?[\w|]*))?(?:\s{2,}(.*?))?\s*$"
)

# Continuation line of an option description: indented to roughly the
# description column (col 28-34) — below the value-marker territory and
# above the option-header indent envelope.
_X265_DESC_CONTINUATION = re.compile(r"^\s{20,}(\S.*?)\s*$")


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


def _extract_value_lists(
    param_cpp: str, level_cpp: str
) -> dict[str, list[tuple[str, str]]]:
    """Return ``{option: [(value, description)]}`` for preset / tune /
    profile, mined from the x265 implementation cascades (the CLI help
    only prints the bare value names with no per-value detail)."""
    out: dict[str, list[tuple[str, str]]] = {}
    if param_cpp:
        clean = _strip_comments(param_cpp)
        body = _find_function_body(clean, "x265_param_default_preset")
        if body:
            presets = _extract_strcmp_cascade(body, "preset")
            if presets:
                out["preset"] = presets
            tunes = _extract_strcmp_cascade(body, "tune")
            if tunes:
                out["tune"] = tunes
    if level_cpp:
        clean = _strip_comments(level_cpp)
        body = _find_function_body(clean, "x265_param_apply_profile")
        if body:
            profiles = _extract_profile_names(body)
            if profiles:
                out["profile"] = profiles
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
    return {
        name: UpstreamOptionHelp(values=values)
        for name, values in _extract_value_lists(param_cpp, level_cpp).items()
    }


def _flatten_x265_help(
    cli_cpp: str,
    defaults: dict[str, int | float | str | None],
    constants: dict[str, int | float | str | None],
    string_tables: dict[str, list[str]],
) -> str:
    """Slice ``showHelp()`` from ``x265cli.cpp`` and return the flat help
    text with printf-style placeholders resolved.

    Same H-call walking as :func:`.x264_help._extract_help_text`, but
    against x265's ``H0``/``H1`` macros (both ``printf``-backed) with
    ``param->X`` field references and the ``OPT()`` bool macro.
    """
    m = re.search(_X265_SHOWHELP_SIG, cli_cpp)
    if not m:
        return ""
    open_brace = m.end() - 1
    # Walk to the matching close brace (string-literal aware).
    depth = 0
    i = open_brace
    n = len(cli_cpp)
    body_end = -1
    while i < n:
        ch = cli_cpp[i]
        if ch == '"':
            i += 1
            while i < n and cli_cpp[i] != '"':
                if cli_cpp[i] == "\\" and i + 1 < n:
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
                body_end = i
                break
        i += 1
    if body_end == -1:
        return ""
    body = cli_cpp[open_brace : body_end + 1]
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(
        r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif|error|warning|pragma)\b[^\n]*\n?",
        "",
        body,
        flags=re.MULTILINE,
    )

    parts: list[str] = []
    for hm in _H_CALL.finditer(body):
        open_paren = hm.end() - 1
        close_paren = _find_matching_paren(body, open_paren)
        if close_paren == -1:
            continue
        inside = body[open_paren + 1 : close_paren]
        comma_parts = _split_top_level_commas(inside)
        if not comma_parts:
            continue
        fmt = "".join(
            _decode_escapes(sm.group(1))
            for sm in _STRING_LITERAL.finditer(comma_parts[0])
        )
        if not fmt:
            continue
        parts.append(
            _resolve_format(
                fmt,
                comma_parts[1:],
                defaults,
                constants,
                string_tables,
                var_name="param",
                opt_macro=True,
            )
        )
    return "".join(parts)


def parse_x265_doc(
    cli_cpp: str,
    param_cpp: str,
    level_cpp: str,
    *,
    common_h: str = "",
    x265_h: str = "",
) -> HelpDoc:
    """Parse x265's CLI help into a section-ordered :class:`HelpDoc`.

    ``cli_cpp`` is ``source/x265cli.cpp`` (the ``showHelp()`` function).
    ``param_cpp`` / ``level_cpp`` supply preset/tune/profile value lists
    (merged onto the matching options). ``common_h`` / ``x265_h`` feed
    the ``#define`` constant table and string-name tables used to
    resolve ``%d`` / ``%s`` placeholders against ``x265_param_default``.
    """
    constants = _parse_constants(common_h + "\n\n" + x265_h)
    defaults = _parse_defaults(param_cpp, constants, _X265_PARAM_DEFAULT_SIG)
    # Name tables (logLevelNames etc.) live in headers + param.h. Pass
    # everything we have; the parser is tolerant of duplicate symbols.
    string_tables = _parse_string_tables(common_h, x265_h, cli_cpp, param_cpp)

    flat = _flatten_x265_help(cli_cpp, defaults, constants, string_tables)
    if not flat:
        return HelpDoc()

    sections: list[HelpSection] = []
    by_name: dict[str, UpstreamOptionHelp] = {}
    current_section: HelpSection | None = None
    current_option: str | None = None
    current_desc_lines: list[str] = []

    def ensure_section() -> HelpSection:
        nonlocal current_section
        if current_section is None:
            current_section = HelpSection(title="General", options=[])
            sections.append(current_section)
        return current_section

    def flush_option() -> None:
        nonlocal current_option, current_desc_lines
        if current_option is not None and current_option not in by_name:
            entry = UpstreamOptionHelp(
                description="\n".join(current_desc_lines).strip()
            )
            by_name[current_option] = entry
            ensure_section().options.append((current_option, entry))
        current_option = None
        current_desc_lines = []

    for line in flat.split("\n"):
        sec = _X265_SECTION.match(line)
        if sec:
            flush_option()
            current_section = HelpSection(title=sec.group(1), options=[])
            sections.append(current_section)
            continue
        opt = _X265_OPTION_HEADER.match(line)
        if opt:
            flush_option()
            current_option = opt.group(1)
            desc = opt.group(2) or ""
            current_desc_lines = [desc] if desc else []
            continue
        if current_option is None:
            continue
        cont = _X265_DESC_CONTINUATION.match(line)
        if cont:
            current_desc_lines.append(cont.group(1))

    flush_option()

    # Merge preset/tune/profile value lists onto their matching options.
    for name, values in _extract_value_lists(param_cpp, level_cpp).items():
        existing = by_name.get(name)
        if existing is not None:
            merged = UpstreamOptionHelp(
                description=existing.description, values=values
            )
            by_name[name] = merged
            for section in sections:
                section.options[:] = [
                    (n, merged if n == name else h) for n, h in section.options
                ]
        else:
            # The CLI help didn't surface this option (unlikely) — drop it
            # into a synthetic Presets section so it still appears.
            entry = UpstreamOptionHelp(values=values)
            by_name[name] = entry
            ensure_section().options.append((name, entry))

    sections = [s for s in sections if s.options]
    return HelpDoc(sections=sections, options=by_name)
