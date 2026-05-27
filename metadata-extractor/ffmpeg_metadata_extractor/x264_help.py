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


_STRING_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"')
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_PREPROC = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif|error|warning|pragma)\b[^\n]*\n?",
    re.MULTILINE,
)

# Option header column-aligned at col 7: ``      --preset <string>      ...``
_OPTION_HEADER = re.compile(r"^\s+--([a-zA-Z][\w-]*)\s+<[^>]+>")

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


def _extract_help_text(c_source: str) -> str:
    """Return the user-visible help text as a single flat string.

    Comments and preprocessor conditionals are stripped so every gated
    branch surfaces (matches the parser policy in :mod:`.avopt_c`); then
    each C string literal in lexical order is decoded and concatenated.
    The C compiler would do the same implicit concatenation between
    adjacent string literals — what we emit here is the printed help
    output as the user would see it with every config flag enabled.
    """
    body = _find_help_body(c_source)
    if not body:
        return ""
    body = _BLOCK_COMMENT.sub(" ", body)
    body = _LINE_COMMENT.sub("", body)
    body = _PREPROC.sub("", body)
    return "".join(_decode_escapes(m.group(1)) for m in _STRING_LITERAL.finditer(body))


def _join_description(lines: list[str]) -> str:
    """Combine continuation lines into one display string.

    x264 wraps long flag-setting lists across multiple help lines (e.g.
    the ``ultrafast`` preset's flag list spans 6 lines). Keeping the
    original ``\\n`` preserves the structure for the SPA's
    ``whitespace: pre-line`` renderer to honor.
    """
    return "\n".join(lines).strip()


def parse_x264_help(c_source: str) -> dict[str, list[tuple[str, str]]]:
    """Return ``{option_name: [(value, description), ...]}`` for every
    ``--<opt> <type>`` option whose body contains value entries.

    In practice this is exactly ``preset``, ``tune``, and ``profile``;
    other options use different help formatting and naturally produce no
    entries. ``description`` may contain embedded ``\\n`` between
    distinct wrapped sub-lines.
    """
    flat = _extract_help_text(c_source)
    if not flat:
        return {}

    results: dict[str, list[tuple[str, str]]] = {}
    current_option: str | None = None
    current_value: str | None = None
    current_lines: list[str] = []
    pending: list[tuple[str, str]] = []

    def flush_value() -> None:
        nonlocal current_value, current_lines
        if current_value is not None:
            desc = _join_description(current_lines)
            pending.append((current_value, desc))
            current_value = None
            current_lines = []

    def flush_option() -> None:
        nonlocal current_option, pending
        if current_option is not None and pending:
            results.setdefault(current_option, []).extend(pending)
        current_option = None
        pending = []

    for line in flat.split("\n"):
        opt_match = _OPTION_HEADER.match(line)
        if opt_match:
            flush_value()
            flush_option()
            current_option = opt_match.group(1)
            continue
        if current_option is None:
            continue

        val_match = _VALUE_LINE.match(line)
        if val_match:
            flush_value()
            current_value = val_match.group(1)
            qualifier = val_match.group(2)
            current_lines = []
            if qualifier:
                # Surface qualifiers like "(psy tuning)" as the first
                # line of the description so the reader sees the grouping.
                current_lines.append(f"({qualifier})")
            continue

        if current_value is not None:
            cont = _CONTINUATION.match(line)
            if cont:
                current_lines.append(cont.group(1))
                continue
            # A non-empty, non-continuation line ends this value's
            # description. Empty lines are tolerated as inner whitespace.
            if line.strip():
                flush_value()

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
