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
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class UpstreamOptionHelp:
    """Per-option help text mined from an upstream library's source.

    ``description``: the option header line (one short paragraph,
    e.g. ``Quality-based VBR (0-51) [23.0]`` from ``--crf`` in x264.c).
    Empty when the upstream source documents the option only as a value
    list (no separate header description) or when the option couldn't be
    found at all.

    ``values``: list of ``(value_name, value_description)`` pairs for
    enum-style options (preset, tune, profile). Empty for options whose
    value is a free-form number/string with no enumerated set.

    The two fields are independent: an option may have only a description
    (``--crf``), only values (``--profile``), both (``--preset``), or
    neither (skip).
    """

    description: str = ""
    values: list[tuple[str, str]] = field(default_factory=list)


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

# Pre-seeded "well-known" symbol values. ``BIT_DEPTH`` is a build-time
# ``-D`` flag (the upstream build ships both 8-bit and 10-bit binaries
# and picks at runtime); pinning to 8 here is the historically dominant
# and most user-visible value. ``INT_MAX`` / ``FLT_MAX`` etc. come from
# ``<limits.h>`` / ``<float.h>`` which we don't parse.
_SEED_CONSTANTS: dict[str, int | float] = {
    "BIT_DEPTH": 8,
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
        parts.append(_resolve_format(fmt, args, defaults, constants))

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


def _to_python_expr(c_expr: str, defaults: dict, constants: dict) -> str | None:
    """Best-effort translation of a C expression to a Python expression
    with names substituted from ``defaults`` and ``constants``.

    Returns ``None`` when the expression contains constructs we don't
    handle (function calls beyond ``X264_MIN``/``X264_MAX``, ternaries
    with un-resolvable operands, etc.).
    """
    # Handle ``X264_MIN(a, b)`` / ``X264_MAX(a, b)`` first — common in
    # x264.c for clamping default ranges. Replace with ``min(a, b)`` /
    # ``max(a, b)`` so Python's built-ins (whitelisted below) take over.
    expr = re.sub(r"\bX264_MIN\b", "min", c_expr)
    expr = re.sub(r"\bX264_MAX\b", "max", expr)

    # Substitute ``defaults->path.to.field`` references.
    def repl_default(m: re.Match[str]) -> str:
        path = m.group(1)
        if path in defaults:
            val = defaults[path]
            return repr(val) if val is not None else "None"
        return f"__UNRESOLVED_{path.replace('.', '_')}"

    expr = re.sub(r"defaults\s*->\s*([\w.]+)", repl_default, expr)

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
    base_c_text: str, constants: dict[str, int | float | str | None]
) -> dict[str, int | float | str | None]:
    """Extract ``param->X.Y = EXPR;`` from ``x264_param_default()`` body.

    Returns ``{"X.Y": value}`` for every assignment whose RHS can be
    resolved using ``constants``. Unresolvable RHS values (function
    calls, ternaries with unknown operands) are skipped — the help-text
    resolver will leave their ``%d``/``%s`` placeholders intact.
    """
    if not base_c_text:
        return {}

    # Find the function body via brace matching.
    sig = re.search(
        r"\bx264_param_default\s*\(\s*x264_param_t\s*\*\s*\w+\s*\)\s*\n*\{",
        base_c_text,
    )
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
        if py_expr is None:
            continue
        val = _safe_eval(py_expr)
        if val is None:
            continue
        out[path] = val
    return out


def _resolve_arg_value(
    arg: str,
    defaults: dict[str, int | float | str | None],
    constants: dict[str, int | float | str | None],
) -> object | None:
    """Try to evaluate one printf argument. Returns ``None`` when
    unresolvable so the caller can leave the placeholder intact."""
    arg = arg.strip()
    if not arg:
        return None
    # Direct ``defaults->X.Y`` access (no further math).
    m = re.fullmatch(r"defaults\s*->\s*([\w.]+)", arg)
    if m:
        return defaults.get(m.group(1))
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
    py_expr = _to_python_expr(arg, defaults, constants)
    if py_expr is None:
        return None
    return _safe_eval(py_expr)


def _resolve_format(
    fmt: str,
    args: list[str],
    defaults: dict[str, int | float | str | None],
    constants: dict[str, int | float | str | None],
) -> str:
    """Walk ``fmt``, substituting each ``%X`` specifier with the
    formatted value of the matching ``args`` entry. Unresolved args
    leave the specifier intact so the reader can still tell something
    was deferred."""
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
                    _resolve_arg_value(args[arg_idx], defaults, constants)
                    if arg_idx < len(args)
                    else None
                )
                if arg_idx < len(args):
                    arg_idx += 1
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
    """
    constants = _parse_constants(common_h + "\n\n" + x264_h)
    defaults = _parse_defaults(base_c, constants)

    flat = _extract_help_text(c_source, defaults=defaults, constants=constants)
    if not flat:
        return {}

    results: dict[str, UpstreamOptionHelp] = {}
    current_option: str | None = None
    current_desc_lines: list[str] = []
    current_value: str | None = None
    current_value_lines: list[str] = []
    pending_values: list[tuple[str, str]] = []
    in_value_block = False  # True after a ``- name:`` line has been seen

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
            if current_option not in results and (description or pending_values):
                results[current_option] = UpstreamOptionHelp(
                    description=description,
                    values=list(pending_values),
                )
        current_option = None
        current_desc_lines = []
        pending_values = []
        in_value_block = False

    for line in flat.split("\n"):
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
    return results


def parse_x264_help_file(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Convenience wrapper: read ``path`` as UTF-8 (replace errors) and
    parse. Returns ``{}`` if the file can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_x264_help(text)
