"""Parse the upstream x264 CLI's verbose help text for the value lists of
``--preset``, ``--tune``, and ``--profile``.

FFmpeg declares these as ``AV_OPT_TYPE_STRING`` and forwards the string
verbatim to ``x264_param_default_preset`` / ``x264_param_default_profile``,
so :mod:`.avopt_c` parses the AVOption rows but emits no enumerated
values. The valid names — and the meaningful description of what each
preset/tune actually sets — live in x264's own ``x264.c`` as concatenated
``H0(...)`` / ``H2(...)`` C string literals inside the ``help()`` function.

This module slices the ``help()`` body, concatenates every string literal
in lexical order (decoding ``\\n`` to real newlines, dropping preprocessor
conditionals so build-config-gated branches all surface), then walks the
resulting flat text looking for the two structural markers x264 uses:

- An option header line, e.g. ``"      --preset <string>      Use a preset..."``.
- A value entry, e.g. ``"                                  - ultrafast:"``
  optionally tagged with a parenthetical qualifier like ``- film (psy tuning):``.

Continuation lines indented under a value are gathered as the value's
description and joined into one string with the original line breaks
preserved (the SPA renders them with ``whitespace: pre-line``).
"""

from __future__ import annotations

import re
from pathlib import Path

# Generic help-doc structures live in ``upstream_help`` so both the x264
# and x265 parsers share one shape (and one HTML renderer). The aliases
# below keep the historical ``X264*`` names working for any in-repo
# references that predate the rename.
from .upstream_help import HelpDoc, HelpSection, UpstreamOptionHelp

X264Section = HelpSection
X264HelpDoc = HelpDoc


_STRING_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"')
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_PREPROC = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif|error|warning|pragma)\b[^\n]*\n?",
    re.MULTILINE,
)

# ``H0(`` / ``H1(`` / ``H2(`` — the help-print macros whose args we mine
# for printf-style argument values (so ``%d`` in the format string can be
# replaced with the actual constant or default value).
_H_CALL = re.compile(r"\bH[012]\s*\(")

# Conversion-specifier matcher used during resolution: matches ``%d``,
# ``%.1f``, ``%-10s``, ``%02d``, etc. ``%%`` is handled explicitly.
_PRINTF_SPEC = re.compile(
    r"%[#0\-+ ]*[0-9]*(?:\.[0-9]+)?[hlLjzt]*[diouxXfFeEgGsc]"
)

# ``#define NAME VALUE`` form, object-like only (no parens after name).
_DEFINE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_(])"
    r"[ \t]+((?:[^\n\\]|\\(?:\r?\n|.))*)\n",
    re.MULTILINE,
)

# A ``param->path.to.field = expression;`` assignment inside
# ``x264_param_default()``. The RHS is captured up to the trailing
# semicolon; multi-line continuations are folded by replacing internal
# whitespace with a single space later.
_PARAM_ASSIGN = re.compile(
    r"param\s*->\s*([A-Za-z_][\w.]*)\s*=\s*([^;]+?)\s*;",
    re.DOTALL,
)

# Header of a string-name table: ``[static] const char * [const]
# x264_<name>_names[] = {``. Both leading ``static`` and the second
# ``const`` are optional in the wild. Captures the array's C symbol
# name (group 1). The body is found by brace-matching after the match.
_STRTABLE_HEAD = re.compile(
    r"\b(?:static\s+)?const\s+char\s*\*\s*(?:const\s+)?"
    r"([A-Za-z_]\w*)\s*\[\s*\]\s*=\s*\{"
)

# Pre-seeded "well-known" symbol values. ``BIT_DEPTH`` is a build-time
# ``-D`` flag (the upstream build ships both 8-bit and 10-bit binaries
# and picks at runtime); pinning to 8 here is the historically dominant
# and most user-visible value. ``INT_MAX`` / ``FLT_MAX`` etc. come from
# ``<limits.h>`` / ``<float.h>`` which we don't parse.
_SEED_CONSTANTS: dict[str, int | float] = {
    "BIT_DEPTH": 8,
    "X265_DEPTH": 8,  # x265's compile-time internal bit depth (build -D flag)
    "INT_MAX": 2_147_483_647,
    "INT_MIN": -2_147_483_648,
    "UINT_MAX": 4_294_967_295,
    "FLT_MAX": 3.4028234663852886e38,
    "FLT_MIN": 1.1754943508222875e-38,
    "DBL_MAX": 1.7976931348623157e308,
    "NULL": None,
}

# Option header column-aligned at col 3-7. Accepts both long-only
# (``--crf``) and short+long (``-q, --qp``) prefixes. The ``<type>``
# argument is optional (some flags like ``--tff`` have none). The
# trailing capture greedily grabs the rest of the header line as the
# option's own description.
#
# The leading-whitespace bound is critical: value-block sub-descriptions
# at col 37 frequently contain lines like ``    --aq-mode 0 --no-psy``
# (referencing other options inside a preset's flag list). Without an
# upper bound the regex would treat those as new option headers and
# steal their text as descriptions. x264.c's actual headers all start
# at col 3 or 7, so 0-12 leading spaces is a safe envelope.
_OPTION_HEADER = re.compile(
    r"^[ \t]{0,12}(?:-\w,\s+)?--([a-zA-Z][\w-]*)(?:\s+<[^>]*>)?(?:\s+(.*?))?\s*$"
)

# Option-description continuation lines: indented to roughly the same
# column as where descriptions start (col 25-34). Above 35 the line is
# probably a value-block ``- name:`` marker or its body.
_OPTION_DESC_CONTINUATION = re.compile(r"^\s{20,34}(\S.*?)\s*$")

# A value entry inside a preset/tune/profile block. Indented to col 35 by
# x264's formatting convention; the parenthetical qualifier (e.g.
# ``(psy tuning)``) is optional.
_VALUE_LINE = re.compile(r"^\s{30,40}-\s+([a-zA-Z][\w-]*)\s*(?:\(([^)]*)\))?\s*:\s*$")

# Continuation lines for a value: indented to col 37+. Anything less
# indented is treated as the end of the value's description.
_CONTINUATION = re.compile(r"^\s{36,}(\S.*?)\s*$")

# Section header line in the help output — a capitalized title at column
# 0 (no leading whitespace) followed by a colon, optionally with no other
# punctuation. Examples in x264.c:
#   ``Presets:``
#   ``Frame-type options:``
#   ``Ratecontrol:``
#   ``Analysis:``
#   ``Input/Output:``
#   ``Filtering:``
# The "Example usage:" line under the synopsis is also matched; the
# parser tolerates it (it doesn't enclose any options, so it stays empty
# and the renderer can drop empty sections).
_SECTION_HEADER = re.compile(r"^([A-Z][A-Za-z][A-Za-z /\-]*?):\s*$")


def _decode_escapes(s: str) -> str:
    """Decode the small subset of C escapes the help strings use."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _find_help_body(text: str) -> str:
    """Slice out the body of ``help(x264_param_t *defaults, int longhelp)``.

    Returns the bytes between the function's opening ``{`` and matching
    ``}`` inclusive, or ``""`` if the function isn't found.
    """
    sig = re.search(
        r"\bhelp\s*\(\s*x264_param_t\s*\*\s*\w+\s*,\s*int\s+\w+\s*\)\s*\n*\{",
        text,
    )
    if not sig:
        return ""
    open_brace = sig.end() - 1
    depth = 0
    i = open_brace
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : i + 1]
        i += 1
    return ""


def _extract_help_text(
    c_source: str,
    defaults: dict[str, int | float | str | None] | None = None,
    constants: dict[str, int | float | str | None] | None = None,
    string_tables: dict[str, list[str]] | None = None,
) -> str:
    """Return the user-visible help text as a single flat string.

    Comments and preprocessor conditionals are stripped so every gated
    branch surfaces, then each ``H0(...)`` / ``H1(...)`` / ``H2(...)``
    macro call's format string is decoded and printf-arg-resolved (when
    ``defaults`` and ``constants`` are supplied), and the results are
    concatenated in source order. The result mirrors what ``x264
    --fullhelp`` would print at runtime — modulo whichever constants we
    couldn't resolve, where the original ``%d`` / ``%s`` etc. stay put.

    When neither resolution map is supplied, this falls back to the
    older "just concatenate every string literal" behavior, useful for
    callers that only need raw header text and don't care about default
    values being baked in.
    """
    body = _find_help_body(c_source)
    if not body:
        return ""
    body = _BLOCK_COMMENT.sub(" ", body)
    body = _LINE_COMMENT.sub("", body)
    body = _PREPROC.sub("", body)

    if defaults is None and constants is None:
        # Legacy path: ignore H<n>() grouping, just concatenate literals.
        return "".join(
            _decode_escapes(m.group(1)) for m in _STRING_LITERAL.finditer(body)
        )

    defaults = defaults or {}
    constants = constants or {}

    parts: list[str] = []
    cursor = 0
    for m in _H_CALL.finditer(body):
        # Any text between H<n>() calls (rare — usually whitespace, but
        # also includes calls we don't recognize) drops out of the
        # output. The flat-text loop above did the same since non-string
        # tokens were skipped by the literal regex.
        cursor = m.end()
        open_paren = m.end() - 1
        close_paren = _find_matching_paren(body, open_paren)
        if close_paren == -1:
            continue
        inside = body[open_paren + 1 : close_paren]
        cursor = close_paren + 1

        # First top-level part is the format (may be ``"foo" "bar"`` —
        # multiple adjacent literals the C compiler concatenates).
        # Remaining parts are the printf args.
        comma_parts = _split_top_level_commas(inside)
        if not comma_parts:
            continue
        fmt_part = comma_parts[0]
        args = comma_parts[1:]
        fmt = "".join(
            _decode_escapes(sm.group(1))
            for sm in _STRING_LITERAL.finditer(fmt_part)
        )
        if not fmt:
            continue
        parts.append(_resolve_format(fmt, args, defaults, constants, string_tables))

    return "".join(parts)


def _join_description(lines: list[str]) -> str:
    """Combine continuation lines into one display string.

    x264 wraps long flag-setting lists across multiple help lines (e.g.
    the ``ultrafast`` preset's flag list spans 6 lines). Keeping the
    original ``\\n`` preserves the structure for the SPA's
    ``whitespace: pre-line`` renderer to honor.
    """
    return "\n".join(lines).strip()


# --- printf-arg resolution -------------------------------------------------


def _split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` on commas at paren/brace/bracket depth 0, honoring
    string/char literals. Returns trimmed parts."""
    parts: list[str] = []
    buf: list[str] = []
    depth_par = depth_brk = depth_brc = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i])
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            if i < n:
                buf.append(text[i])
                i += 1
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i])
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            if i < n:
                buf.append(text[i])
                i += 1
            continue
        if ch == "(":
            depth_par += 1
        elif ch == ")":
            depth_par -= 1
        elif ch == "[":
            depth_brk += 1
        elif ch == "]":
            depth_brk -= 1
        elif ch == "{":
            depth_brc += 1
        elif ch == "}":
            depth_brc -= 1
        elif ch == "," and depth_par == 0 and depth_brk == 0 and depth_brc == 0:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Given ``text[open_idx] == '('``, return the matching ``)`` or -1.
    String/char literals don't perturb the count."""
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
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _to_python_expr(
    c_expr: str, defaults: dict, constants: dict, var_name: str = "defaults"
) -> str | None:
    """Best-effort translation of a C expression to a Python expression
    with names substituted from ``defaults`` and ``constants``.

    ``var_name`` is the C variable that holds the default-bearing struct
    pointer in the source being parsed — ``defaults`` for x264.c's
    ``help()``, ``param`` for x265cli.cpp's ``showHelp()``.

    Returns ``None`` when the expression contains constructs we don't
    handle (function calls beyond ``X264_MIN``/``X264_MAX``/``X265_MIN``/
    ``X265_MAX``, ternaries with un-resolvable operands, etc.).
    """
    # Handle ``{X264,X265}_MIN(a, b)`` / ``_MAX(a, b)`` first — common in
    # both projects for clamping default ranges. Replace with Python's
    # ``min(a, b)`` / ``max(a, b)`` (whitelisted in :func:`_safe_eval`).
    expr = re.sub(r"\bX26[45]_MIN\b", "min", c_expr)
    expr = re.sub(r"\bX26[45]_MAX\b", "max", expr)

    # Substitute ``<var_name>->path.to.field`` references.
    def repl_default(m: re.Match[str]) -> str:
        path = m.group(1)
        if path in defaults:
            val = defaults[path]
            return repr(val) if val is not None else "None"
        return f"__UNRESOLVED_{path.replace('.', '_')}"

    expr = re.sub(
        rf"{re.escape(var_name)}\s*->\s*([\w.]+)", repl_default, expr
    )

    # Substitute bare ALL_CAPS identifiers (C-style constants).
    def repl_const(m: re.Match[str]) -> str:
        name = m.group(0)
        if name in constants:
            val = constants[name]
            return repr(val) if val is not None else "None"
        # ``min``/``max`` are now the rewritten X264_MIN/MAX; leave them
        # alone so Python's built-ins resolve.
        if name in ("min", "max"):
            return name
        return f"__UNRESOLVED_{name}"

    expr = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\b", repl_const, expr)

    # Translate C ternary ``a ? b : c`` → Python ``(b if a else c)``.
    # Iterative because ternaries can nest.
    for _ in range(4):
        new = re.sub(
            r"([^?:]+?)\s*\?\s*([^?:]+?)\s*:\s*([^?:]+)",
            r"((\2) if (\1) else (\3))",
            expr,
        )
        if new == expr:
            break
        expr = new

    if "__UNRESOLVED_" in expr:
        return None
    return expr


def _safe_eval(py_expr: str) -> int | float | str | None:
    """Evaluate a Python expression in a tiny sandbox. Returns ``None``
    on any failure (syntax error, unsupported builtin, etc.)."""
    try:
        return eval(
            py_expr,
            {"__builtins__": {"min": min, "max": max}},
            {},
        )
    except Exception:
        return None


def _parse_constants(headers_text: str) -> dict[str, int | float | str | None]:
    """Walk ``#define NAME VALUE`` lines and return resolved values.

    Multi-pass resolution: each pass tries to evaluate definitions
    whose RHS now contains only known names; loop stops when a pass
    resolves nothing new. Definitions that never resolve (function
    calls, type-cast expressions, etc.) stay out of the result so
    callers know they're unknown.
    """
    if not headers_text:
        return dict(_SEED_CONSTANTS)

    # Strip C comments first — many ``#define`` lines carry a trailing
    # ``/* ... */`` (e.g. x265's ``#define QP_MAX_MAX 69 /* ... */``).
    # Leaving them in makes the RHS un-evaluable. Block comments are
    # removed before line comments so a ``//`` inside ``/* */`` is safe.
    headers_text = _BLOCK_COMMENT.sub(" ", headers_text)
    headers_text = _LINE_COMMENT.sub("", headers_text)

    raw: dict[str, str] = {}
    for m in _DEFINE.finditer(headers_text):
        name = m.group(1)
        body = re.sub(r"\\\r?\n", " ", m.group(2)).strip()
        if not body:
            continue
        raw[name] = body

    resolved: dict[str, int | float | str | None] = dict(_SEED_CONSTANTS)
    pending = dict(raw)
    while True:
        progress = False
        for name in list(pending):
            if name in resolved:
                pending.pop(name)
                progress = True
                continue
            body = pending[name]
            # String-literal #define (``#define X "foo"``) — emit as-is.
            sm = re.fullmatch(r'"((?:\\.|[^"\\])*)"', body)
            if sm:
                resolved[name] = sm.group(1)
                pending.pop(name)
                progress = True
                continue
            py_expr = _to_python_expr(body, {}, resolved)
            if py_expr is None:
                continue
            val = _safe_eval(py_expr)
            if val is None:
                continue
            resolved[name] = val
            pending.pop(name)
            progress = True
        if not progress:
            break
    return resolved


def _parse_defaults(
    base_c_text: str,
    constants: dict[str, int | float | str | None],
    func_sig: str = r"\bx264_param_default\s*\(\s*x264_param_t\s*\*\s*\w+\s*\)\s*\n*\{",
) -> dict[str, int | float | str | None]:
    """Extract ``param->X.Y = EXPR;`` from ``x264_param_default()`` body.

    ``func_sig`` is the regex that locates the defaults function's
    opening brace; the x265 caller passes the ``x265_param_default``
    signature instead.

    Returns ``{"X.Y": value}`` for every assignment seen, with the
    resolved value or ``None`` when the RHS can't be evaluated (function
    calls, ternaries with unknown operands, etc.). The distinction
    between "mentioned but inevaluable" and "not mentioned at all" is
    semantically meaningful at the resolution layer:

    - Mentioned + evaluable → real default value.
    - Mentioned + ``None`` → genuinely runtime-determined; the printf
      placeholder stays literal.
    - **Absent from the dict** → x264 relies on the function-entry
      ``memset(param, 0, sizeof(x264_param_t))`` to zero the field.
      :func:`_resolve_format` substitutes zero of the placeholder's
      type (``0`` for ``%d``, ``0.0`` for ``%f``, ``""`` for ``%s``).
    """
    if not base_c_text:
        return {}

    # Find the function body via brace matching.
    sig = re.search(func_sig, base_c_text)
    if not sig:
        return {}
    open_brace = sig.end() - 1
    depth = 0
    i = open_brace
    n = len(base_c_text)
    while i < n:
        ch = base_c_text[i]
        if ch == '"':
            i += 1
            while i < n and base_c_text[i] != '"':
                if base_c_text[i] == "\\" and i + 1 < n:
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
                break
        i += 1
    body = base_c_text[open_brace : i + 1]
    body = _BLOCK_COMMENT.sub(" ", body)
    body = _LINE_COMMENT.sub("", body)
    # Strip preprocessor conditionals — every branch (e.g. bit-depth
    # gated defaults) surfaces. Matches the policy used elsewhere.
    body = _PREPROC.sub("", body)

    out: dict[str, int | float | str | None] = {}
    for m in _PARAM_ASSIGN.finditer(body):
        path = m.group(1)
        rhs = re.sub(r"\s+", " ", m.group(2).strip())
        # String literal RHS.
        sm = re.fullmatch(r'"((?:\\.|[^"\\])*)"', rhs)
        if sm:
            out[path] = sm.group(1)
            continue
        py_expr = _to_python_expr(rhs, {}, constants)
        val = None if py_expr is None else _safe_eval(py_expr)
        # ``None`` is recorded too — it means "x264 explicitly sets this
        # field, but to something we can't evaluate" (runtime call,
        # ternary on an unknown). The resolver treats that differently
        # from a field absent from the dict (memset-zeroed).
        out[path] = val
    return out


def _parse_string_tables(*sources: str) -> dict[str, list[str]]:
    """Extract ``[static] const char * [const] NAME[] = { "s1", …, 0 };``
    arrays from one or more sources.

    Returns ``{NAME: [s1, s2, …]}`` — the terminating ``0`` / ``NULL``
    sentinel is dropped naturally (we collect only string literals from
    the body). Used to resolve ``strtable_lookup(NAME, defaults->X)``
    references in the help text: the integer default of ``X`` becomes
    the index into the returned list.

    The tables live in both ``x264.h`` (most enum-name tables) and
    ``x264.c`` (a handful like ``x264_cqm_names``, ``x264_pulldown_names``),
    so callers pass both sources concatenated.
    """
    out: dict[str, list[str]] = {}
    for source in sources:
        if not source:
            continue
        cleaned = _BLOCK_COMMENT.sub(" ", source)
        cleaned = _LINE_COMMENT.sub("", cleaned)
        for m in _STRTABLE_HEAD.finditer(cleaned):
            name = m.group(1)
            if name in out:
                continue  # first-wins so a later partial declaration can't shadow
            open_brace = cleaned.find("{", m.end() - 1)
            if open_brace == -1:
                continue
            # Walk to matching close brace, honoring string literals so a
            # ``{`` inside ``"..."`` doesn't perturb the depth.
            depth = 0
            i = open_brace
            n = len(cleaned)
            while i < n:
                ch = cleaned[i]
                if ch == '"':
                    i += 1
                    while i < n and cleaned[i] != '"':
                        if cleaned[i] == "\\" and i + 1 < n:
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
                        body = cleaned[open_brace + 1 : i]
                        out[name] = [
                            _decode_escapes(sm.group(1))
                            for sm in _STRING_LITERAL.finditer(body)
                        ]
                        break
                i += 1
    return out


# Sentinel value returned by :func:`_resolve_arg_value` when a
# ``defaults->X`` reference points at a struct field that
# ``x264_param_default()`` never explicitly assigns. C-struct semantics
# guarantee such a field has been zeroed by the function-entry
# ``memset(param, 0, sizeof(x264_param_t))``, so the resolver
# substitutes zero of the printf placeholder's type (``0`` for ``%d``,
# ``0.0`` for ``%f``, ``""`` for ``%s``). Distinct from ``None``, which
# means "field IS assigned but the RHS expression is runtime-determined
# (e.g. ``x264_cpu_detect()``)" — those stay literal in the rendered
# help text.
class _MemsetDefault:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — purely for debugging
        return "<MEMSET_DEFAULT>"


_MEMSET_DEFAULT = _MemsetDefault()


def _resolve_arg_value(
    arg: str,
    defaults: dict[str, int | float | str | None],
    constants: dict[str, int | float | str | None],
    string_tables: dict[str, list[str]] | None = None,
    var_name: str = "defaults",
    opt_macro: bool = False,
) -> object | None:
    """Try to evaluate one printf argument.

    ``var_name`` is the default-bearing struct pointer in the source
    being parsed (``defaults`` for x264, ``param`` for x265).
    ``opt_macro`` enables x265's ``OPT(param->X)`` wrapper, which maps a
    boolean field to ``"enabled"`` / ``"disabled"``.

    Return values carry three-way information:

    - Resolved value (int, float, str) → use directly.
    - ``None`` → arg references a runtime-determined value (or is an
      expression we can't simplify); leave the placeholder literal.
    - :data:`_MEMSET_DEFAULT` → arg references a struct field that the
      ``*_param_default()`` function left untouched, so it inherits zero
      from the function-entry memset. :func:`_resolve_format`
      substitutes zero of the format spec's type.
    """
    arg = arg.strip()
    if not arg:
        return None
    tables = string_tables or {}

    # ``OPT( param->X )`` — x265's boolean-rendering macro
    # (``#define OPT(value) (value ? "enabled" : "disabled")``). Resolve
    # the inner field default and map truthiness to the two strings. A
    # memset-zeroed field counts as disabled.
    if opt_macro:
        om = re.fullmatch(r"OPT\s*\(\s*(.+?)\s*\)", arg)
        if om:
            inner = _resolve_arg_value(
                om.group(1), defaults, constants, string_tables,
                var_name=var_name, opt_macro=False,
            )
            if inner is _MEMSET_DEFAULT:
                return "disabled"
            if inner is None:
                return None
            try:
                return "enabled" if inner else "disabled"
            except Exception:
                return None

    # Strip a C++ namespace qualifier (``X265_NS::logLevelNames`` →
    # ``logLevelNames``) so the table/expression handlers below see a
    # bare symbol. Only done for the leading qualifier of the whole arg.
    arg = re.sub(r"^(?:\w+::)+", "", arg)

    # ``strtable_lookup( table_name, <var>->X )`` — x264 emits this in
    # many ``%s`` printf args to translate an integer enum value to the
    # human-readable name. Resolve it by combining a parsed name table
    # (group 1) with the integer default of the indexed field (group 2).
    # Memset-zeroed indices land at table[0], which is the right answer
    # in every case x264 follows the convention "enum value 0 is the
    # documented default".
    sm = re.fullmatch(
        rf"strtable_lookup\s*\(\s*(\w+)\s*,\s*{re.escape(var_name)}"
        r"\s*->\s*([\w.]+)\s*\)",
        arg,
    )
    if sm:
        table_name = sm.group(1)
        path = sm.group(2)
        table = tables.get(table_name)
        if table is None:
            return None
        if path in defaults:
            idx_val = defaults[path]
        else:
            idx_val = 0  # memset-zeroed → index 0
        if not isinstance(idx_val, int):
            return None
        if 0 <= idx_val < len(table):
            return table[idx_val]
        # Out-of-range index — matches x264's own ``strtable_lookup()``
        # which prints ``"???"`` rather than crashing. Happens when the
        # field's default is a sentinel like ``-1`` meaning "depends on
        # input" (e.g. ``vui.i_colmatrix``).
        return "???"

    # ``stringify_names( buf, table_name )`` — x264's helper for
    # rendering an entire name table as ``"a, b, c"`` (the ``buf``
    # parameter is just a scratch buffer the helper writes into).
    # Resolve by joining the table's entries.
    sm = re.fullmatch(
        r"stringify_names\s*\(\s*\w+\s*,\s*(\w+)\s*\)",
        arg,
    )
    if sm:
        table = tables.get(sm.group(1))
        return ", ".join(table) if table is not None else None

    # ``table_name[INDEX]`` — indexing of a name table. x264 uses a
    # literal index (``x264_muxer_names[0]``); x265 uses an expression
    # (``logLevelNames[param->logLevel + 1]``). Resolve the index via
    # the expression evaluator so both forms work.
    sm = re.fullmatch(r"(\w+)\s*\[\s*(.+?)\s*\]", arg)
    if sm:
        table = tables.get(sm.group(1))
        if table is not None:
            idx_expr = sm.group(2)
            if re.fullmatch(r"\d+", idx_expr):
                idx = int(idx_expr)
            else:
                py = _to_python_expr(idx_expr, defaults, constants, var_name)
                idx = _safe_eval(py) if py is not None else None
            if isinstance(idx, int) and 0 <= idx < len(table):
                return table[idx]
            return None

    # Direct ``<var>->X.Y`` or ``<var>->X.Y[N]`` access. Array indexing
    # matters for fields like ``analyse.i_luma_deadzone[0]`` — the regex
    # only captures the path up to (but not including) the bracket; the
    # index is captured separately. When the path is present in defaults
    # and its value is a list/tuple, return the indexed element;
    # otherwise fall through to the memset-default logic (array fields
    # never explicitly assigned are zeroed just like scalars).
    m = re.fullmatch(rf"{re.escape(var_name)}\s*->\s*([\w.]+)(?:\[(\d+)\])?", arg)
    if m:
        path = m.group(1)
        index_str = m.group(2)
        if path in defaults:
            value = defaults[path]
            if index_str is not None and isinstance(value, (list, tuple)):
                idx = int(index_str)
                if 0 <= idx < len(value):
                    return value[idx]
                return _MEMSET_DEFAULT
            # Either a resolved value or None (mentioned-but-inevaluable).
            return value
        # Not mentioned in the *_param_default() function → memset-zeroed
        # at function entry; surface that distinction to the caller.
        return _MEMSET_DEFAULT
    # Bare numeric literal.
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    if re.fullmatch(r"-?\d+\.\d*([eE][+-]?\d+)?", arg):
        return float(arg)
    # Bare string literal.
    sm = re.fullmatch(r'"((?:\\.|[^"\\])*)"', arg)
    if sm:
        return sm.group(1)
    # Bare identifier (constant).
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", arg) and arg in constants:
        return constants[arg]
    # Expression — substitute and try to eval.
    py_expr = _to_python_expr(arg, defaults, constants, var_name)
    if py_expr is None:
        return None
    return _safe_eval(py_expr)


def _zero_for_spec(spec: str) -> int | float | str:
    """The "C zero" for a printf conversion specifier.

    ``memset(0)`` of a struct gives every field its type-appropriate
    zero — an int field reads as ``0``, a float reads as ``0.0``, a
    ``char *`` reads as ``NULL`` (which ``printf("%s", NULL)`` renders
    as ``"(null)"`` in glibc but as the empty string in many other
    libcs; we go with empty string as the more readable choice).
    """
    last = spec[-1]
    if last in "fFeEgG":
        return 0.0
    if last in "sc":
        return ""
    # diouxX and anything else → integer 0.
    return 0


def _resolve_format(
    fmt: str,
    args: list[str],
    defaults: dict[str, int | float | str | None],
    constants: dict[str, int | float | str | None],
    string_tables: dict[str, list[str]] | None = None,
    var_name: str = "defaults",
    opt_macro: bool = False,
) -> str:
    """Walk ``fmt``, substituting each ``%X`` specifier with the
    formatted value of the matching ``args`` entry. Unresolved args
    leave the specifier intact so the reader can still tell something
    was deferred.

    ``var_name`` / ``opt_macro`` are forwarded to
    :func:`_resolve_arg_value` for x265's ``param->X`` references and
    ``OPT()`` macro."""
    out: list[str] = []
    arg_idx = 0
    i = 0
    n = len(fmt)
    while i < n:
        if fmt[i] == "%" and i + 1 < n and fmt[i + 1] == "%":
            out.append("%")
            i += 2
            continue
        if fmt[i] == "%":
            m = _PRINTF_SPEC.match(fmt, i)
            if m:
                spec = m.group(0)
                value = (
                    _resolve_arg_value(
                        args[arg_idx], defaults, constants, string_tables,
                        var_name=var_name, opt_macro=opt_macro,
                    )
                    if arg_idx < len(args)
                    else None
                )
                if arg_idx < len(args):
                    arg_idx += 1
                # Memset-default: substitute zero of the spec's type.
                if value is _MEMSET_DEFAULT:
                    value = _zero_for_spec(spec)
                if value is not None:
                    try:
                        out.append(spec % value)
                    except (TypeError, ValueError):
                        out.append(spec)
                else:
                    out.append(spec)
                i += len(spec)
                continue
        out.append(fmt[i])
        i += 1
    return "".join(out)


def parse_x264_doc(
    c_source: str,
    *,
    base_c: str = "",
    common_h: str = "",
    x264_h: str = "",
) -> X264HelpDoc:
    """Parse x264's help text into a section-ordered doc structure.

    The returned :class:`X264HelpDoc` carries both an ordered list of
    sections (each with its options in source order, for doc rendering)
    and a flat by-name index (for the extractor's option-overlay step).
    Both views share the same :class:`UpstreamOptionHelp` instances.

    ``base_c`` / ``common_h`` / ``x264_h`` are optional auxiliary sources
    consulted for placeholder resolution — see :func:`parse_x264_help`.
    """
    constants = _parse_constants(common_h + "\n\n" + x264_h)
    defaults = _parse_defaults(base_c, constants)
    # Name tables can live in either x264.h (most enum-name tables) or
    # x264.c (a handful — cqm, pulldown, range, …). Walk both.
    string_tables = _parse_string_tables(c_source, x264_h)

    flat = _extract_help_text(
        c_source,
        defaults=defaults,
        constants=constants,
        string_tables=string_tables,
    )
    if not flat:
        return X264HelpDoc()

    sections: list[X264Section] = []
    by_name: dict[str, UpstreamOptionHelp] = {}

    # Per-option parsing state.
    current_section: X264Section | None = None
    current_option: str | None = None
    current_desc_lines: list[str] = []
    current_value: str | None = None
    current_value_lines: list[str] = []
    pending_values: list[tuple[str, str]] = []
    in_value_block = False  # True after a ``- name:`` line has been seen

    def ensure_section() -> X264Section:
        """Return the current section, creating an unnamed catch-all for
        options that appear before the first section header (rare —
        usually the synopsis block)."""
        nonlocal current_section
        if current_section is None:
            current_section = X264Section(title="General", options=[])
            sections.append(current_section)
        return current_section

    def flush_value() -> None:
        nonlocal current_value, current_value_lines
        if current_value is not None:
            desc = _join_description(current_value_lines)
            pending_values.append((current_value, desc))
            current_value = None
            current_value_lines = []

    def flush_option() -> None:
        nonlocal current_option, current_desc_lines, pending_values, in_value_block
        if current_option is not None:
            description = _join_description(current_desc_lines)
            # Don't overwrite a richer earlier entry. The help text never
            # repeats option headers in practice, but the guard keeps
            # behavior stable if a future x264 reorganization changes that.
            if current_option not in by_name and (description or pending_values):
                entry = UpstreamOptionHelp(
                    description=description,
                    values=list(pending_values),
                )
                by_name[current_option] = entry
                ensure_section().options.append((current_option, entry))
        current_option = None
        current_desc_lines = []
        pending_values = []
        in_value_block = False

    for line in flat.split("\n"):
        # Section header — Capitalized text + colon at column 0. Must
        # match BEFORE the option-header check so a section heading
        # isn't misread as an option (the option-header regex also
        # tolerates col 0, though that combo is unusual in practice).
        sec_match = _SECTION_HEADER.match(line)
        if sec_match:
            flush_value()
            flush_option()
            current_section = X264Section(title=sec_match.group(1), options=[])
            sections.append(current_section)
            continue

        opt_match = _OPTION_HEADER.match(line)
        if opt_match:
            flush_value()
            flush_option()
            current_option = opt_match.group(1)
            header_desc = opt_match.group(2) or ""
            current_desc_lines = [header_desc] if header_desc else []
            continue
        if current_option is None:
            continue

        val_match = _VALUE_LINE.match(line)
        if val_match:
            flush_value()
            in_value_block = True
            current_value = val_match.group(1)
            qualifier = val_match.group(2)
            current_value_lines = []
            if qualifier:
                # Surface qualifiers like "(psy tuning)" as the first
                # line of the description so the reader sees the grouping.
                current_value_lines.append(f"({qualifier})")
            continue

        if current_value is not None:
            cont = _CONTINUATION.match(line)
            if cont:
                current_value_lines.append(cont.group(1))
                continue
            # A non-empty, non-continuation line ends this value's
            # description. Empty lines are tolerated as inner whitespace.
            if line.strip():
                flush_value()
            continue

        # No value block yet for this option — treat indented lines as
        # continuation of the option's own description.
        if not in_value_block:
            cont = _OPTION_DESC_CONTINUATION.match(line)
            if cont:
                current_desc_lines.append(cont.group(1))

    flush_value()
    flush_option()

    # Drop sections that ended up with no options (e.g. "Example usage").
    sections = [s for s in sections if s.options]
    return X264HelpDoc(sections=sections, options=by_name)


def parse_x264_help(
    c_source: str,
    *,
    base_c: str = "",
    common_h: str = "",
    x264_h: str = "",
) -> dict[str, UpstreamOptionHelp]:
    """Return ``{long_option_name: UpstreamOptionHelp}`` for every
    ``--<opt>`` declared in the help text.

    Each entry carries the option's own header description (always
    present when the header parses) plus a value list for the few
    options that emit one (``--preset``, ``--tune``, ``--profile``).

    The extractor matches FFmpeg libx264 options against this map by
    bare name (option ``-crf`` looks up key ``"crf"``), so a richer
    upstream description automatically reaches every libx264 option
    whose name happens to coincide with x264's CLI option spelling —
    no hardcoded mapping table required.

    When the optional auxiliary sources are supplied, printf-style
    placeholders in the help text are resolved against ``#define``
    constants (from the headers) and ``x264_param_default()`` field
    initializers (from ``common/base.c``). So ``Force constant QP
    (0-%d, 0=lossless)`` becomes ``Force constant QP (0-69, 0=lossless)``
    (with ``BIT_DEPTH=8``, the default ``QP_MAX`` works out to 69).
    Placeholders whose arguments don't resolve (function calls,
    ternaries with unknown operands) stay literal.

    Thin wrapper over :func:`parse_x264_doc`, which is the richer
    section-aware parser used by the HTML doc renderer.
    """
    return parse_x264_doc(
        c_source, base_c=base_c, common_h=common_h, x264_h=x264_h
    ).options


def parse_x264_help_file(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Convenience wrapper: read ``path`` as UTF-8 (replace errors) and
    parse. Returns ``{}`` if the file can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_x264_help(text)
