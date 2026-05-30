"""Parse FFmpeg's C ``OptionDef`` tables to fill gaps in the texi docs.

FFmpeg's ``OptionDef options[]`` table in ``fftools/ffmpeg_opt.c`` (and the
``CMDUTILS_COMMON_OPTIONS`` macro in ``fftools/opt_common.h`` /
``fftools/cmdutils.h``) is the authoritative registry of every CLI option
the ``ffmpeg`` binary accepts. The texi docs are a curated subset of that
registry — they describe most options well but leave some out entirely
(e.g. ``-hwaccel_output_format``) and define several short/legacy
alternative names that share a backing handler with a "canonical" option
without getting their own doc entry.

This module produces two pieces of information from the C source:

1. **Aliases for documented options** — :func:`build_alias_map` returns
   ``{canonical_dash_name: [alias_dash_name, ...]}`` and
   :func:`apply_alias_map` folds each entry into the corresponding doc-derived
   option's ``aliases`` list. Common cases:

   - ``apre`` / ``vpre`` / ``spre`` → alias of ``pre``    (via ``name_canon``)
   - ``stag``                       → alias of ``tag``    (via ``name_canon``)
   - ``scodec`` / ``dcodec``        → alias of ``codec``  (via ``names_alt``)
   - ``lavfi``                      → alias of ``filter_complex`` (in older
     tags, where ``-lavfi`` lacks its own doc entry; same ``.func_arg``)

2. **Top-level entries for fully-undocumented options** —
   :func:`build_undocumented_options` returns synthesized option dicts
   (matching the texi-parser shape) for every name in the C table that
   is neither documented nor recognized as an alias of a documented option.
   Scope / valueType / signature are inferred from the ``OPT_*`` flag
   tokens; the description is the short C help string. This is *gap-fill*
   only — a name that is also present in the texi docs always keeps its
   richer doc-derived entry; the C-source description never overrides it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Matches the start of any ``{ "name", ...`` OptionDef-style entry. Accepts
# leading ``-`` and ``?`` so CMDUTILS_COMMON_OPTIONS entries like ``"-help"``
# and ``"?"`` are recognized too.
_ENTRY_NAME_RE = re.compile(r'\{\s*"(-?[A-Za-z_?][A-Za-z_0-9?\-]*)"\s*,')

_NAME_CANON_RE = re.compile(r'\.u1\.name_canon\s*=\s*"([^"]+)"')
_NAMES_ALT_RE = re.compile(r'\.u1\.names_alt\s*=\s*([A-Za-z_][A-Za-z_0-9]*)\b')

# ``static const char *const NAME[] = { "a", "b", NULL };`` — captures the
# array name and its string contents (NULL terminator and whitespace
# tolerated).
_ALT_ARRAY_RE = re.compile(
    r'(?:\bstatic\s+)?\bconst\s+char\s*\*\s*const\s+(\w+)\s*\[\s*\]\s*='
    r'\s*\{([^}]*)\}\s*;'
)


def _strip_c_comments(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments while preserving string literals.

    A naive regex strip would corrupt strings that contain ``//`` or ``/*``,
    so we walk the text character by character, copying string literals
    verbatim and dropping comment ranges.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            nl = text.find('\n', i)
            i = n if nl < 0 else nl
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _skip_string(text: str, i: int) -> int:
    """Given ``text[i]`` is an opening quote, return index past the closer."""
    quote = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == '\\' and j + 1 < n:
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return j


def _match_brace(text: str, i: int) -> int | None:
    """Index of the ``}`` matching ``text[i] == '{'``, or None if unbalanced."""
    depth = 0
    j = i
    n = len(text)
    while j < n:
        ch = text[j]
        if ch in '"\'':
            j = _skip_string(text, j)
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _parse_alt_arrays(text: str) -> dict[str, list[str]]:
    """Return ``{array_name: [alt_name, ...]}`` for every ``alt_X[]`` array."""
    out: dict[str, list[str]] = {}
    for m in _ALT_ARRAY_RE.finditer(text):
        names = re.findall(r'"([^"]+)"', m.group(2))
        out[m.group(1)] = names
    return out


@dataclass(frozen=True)
class _RawEntry:
    """One parsed ``{ "name", flags, { sink } [, "help", "argname"] }`` row.

    ``flags_text`` is the raw expression between the name's trailing comma
    and the union opener — i.e. the ``OPT_TYPE_*`` / scope-flag region.
    ``help_text`` is the third C-string argument (the short description);
    ``arg_name`` is the fourth (the placeholder used in the documented
    invocation form, e.g. ``"format"``). Both default to ``""`` when the
    entry omits them.
    """

    name: str
    sink: str
    canon: str | None
    alt: str | None
    flags_text: str
    help_text: str
    arg_name: str


def _read_c_string(text: str, i: int) -> tuple[str, int]:
    """Given ``text[i] == '"'``, return ``(decoded_content, index_past_close)``.

    Decodes the common C escapes (``\\n``, ``\\t``, ``\\r``, ``\\"``, ``\\\\``);
    any other ``\\x`` sequence is passed through as the escaped character.
    """
    j = i + 1
    n = len(text)
    buf: list[str] = []
    escapes = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '0': ''}
    while j < n:
        c = text[j]
        if c == '\\' and j + 1 < n:
            buf.append(escapes.get(text[j + 1], text[j + 1]))
            j += 2
            continue
        if c == '"':
            return ''.join(buf), j + 1
        buf.append(c)
        j += 1
    return ''.join(buf), j


def _read_tail_strings(tail: str) -> list[str]:
    """Extract comma-separated C-string arguments from ``tail``.

    Adjacent string literals separated only by whitespace are concatenated
    (per C). Designation initializers (``.u1.name_canon = "X"``) are
    excluded — only positional string arguments at the top of the entry
    tail are returned. Stops collecting once a non-string, non-whitespace
    positional argument is seen.
    """
    out: list[str] = []
    current: list[str] = []
    in_arg = False
    saw_designator = False
    i = 0
    n = len(tail)
    while i < n:
        ch = tail[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '.':
            # Designated initializer — stop collecting positional args.
            saw_designator = True
            break
        if ch == '"':
            content, end = _read_c_string(tail, i)
            current.append(content)
            in_arg = True
            i = end
            continue
        if ch == ',':
            if in_arg:
                out.append(''.join(current))
                current = []
                in_arg = False
            i += 1
            continue
        # Some other token (identifier, NULL, ...) — abort collection so
        # we don't misinterpret it as a missing string.
        break
    if in_arg and not saw_designator:
        out.append(''.join(current))
    return out


def _parse_entries(text: str) -> list[_RawEntry]:
    """Walk every ``{ "name", flags, { sink } [, "help", "argname" ...] }`` entry.

    Returns one :class:`_RawEntry` per row. ``sink`` is the union body
    (the second ``{...}``) with whitespace stripped — sufficient to detect
    entries that share a backing handler (same ``.func_arg`` symbol or same
    ``OFFSET(field)``).

    Entries without a parseable union body are skipped silently — the C
    source includes macro lines (``CMDUTILS_COMMON_OPTIONS``) and stray
    string-pairs in unrelated tables that this parser shouldn't fail over.
    """
    out: list[_RawEntry] = []
    n = len(text)
    for m in _ENTRY_NAME_RE.finditer(text):
        name = m.group(1)
        flags_start = m.end()
        i = flags_start
        union_open: int | None = None
        # Walk forward through the flags expression until we hit the union
        # opener. Stops if the entry closes first (malformed/skipped).
        while i < n:
            ch = text[i]
            if ch in '"\'':
                i = _skip_string(text, i)
                continue
            if ch == '{':
                union_open = i
                break
            if ch == '}':
                break
            i += 1
        if union_open is None:
            continue
        union_close = _match_brace(text, union_open)
        if union_close is None:
            continue
        sink = re.sub(r'\s+', '', text[union_open + 1:union_close])
        if not sink:
            continue
        flags_text = text[flags_start:union_open].strip().rstrip(',').strip()

        # Scan the trailing tail (between the union close and the entry's
        # own close brace) for ``.u1.name_canon`` / ``.u1.names_alt``.
        depth = 0
        j = union_close + 1
        entry_close: int | None = None
        while j < n:
            ch = text[j]
            if ch in '"\'':
                j = _skip_string(text, j)
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                if depth == 0:
                    entry_close = j
                    break
                depth -= 1
            j += 1
        tail = text[union_close + 1:entry_close] if entry_close is not None else ""
        canon_m = _NAME_CANON_RE.search(tail)
        alt_m = _NAMES_ALT_RE.search(tail)
        # The positional args start with a leading comma after the union
        # close; strip it before splitting so ``_read_tail_strings`` sees
        # the first string at offset 0.
        positional = tail.lstrip()
        if positional.startswith(','):
            positional = positional[1:]
        strings = _read_tail_strings(positional)
        out.append(
            _RawEntry(
                name=name,
                sink=sink,
                canon=canon_m.group(1) if canon_m else None,
                alt=alt_m.group(1) if alt_m else None,
                flags_text=flags_text,
                help_text=strings[0] if len(strings) >= 1 else "",
                arg_name=strings[1] if len(strings) >= 2 else "",
            )
        )
    return out


def build_alias_map(
    sources: dict[str, str], documented: set[str]
) -> dict[str, list[str]]:
    """Discover ``{ "-canonical": ["-alias", ...] }`` from C sources.

    ``sources`` is a ``{path: text}`` map of C files to scan together (the
    main options table plus the common-options macro header). Comments are
    stripped per file; the bodies are then walked as one combined stream so
    that an entry's ``.u1.name_canon = "X"`` reference resolves whether ``X``
    lives in the same file or another.

    ``documented`` is the set of option names *with* leading dash that the
    texi-based extractor already produced (e.g. ``{"-vf", "-codec", ...}``).
    Aliases are only attached to options that appear in this set, and only
    names that are absent from it become aliases — documented entries keep
    their independent positions.

    Three signals, applied independently rather than transitively merged
    (so that semantically-distinct siblings don't end up aliased to one
    another):

    1. **``name_canon``**: ``E`` with ``.u1.name_canon = "X"`` is an alias
       of ``X``. Added when ``-X`` is documented and ``-E.name`` is not.
    2. **``names_alt``**: ``E`` with ``.u1.names_alt = alt_X`` claims
       ``alt_X[]`` as its alternates. Added when ``-E.name`` is documented
       — each alt is attached as an alias of ``E.name`` if undocumented.
    3. **Shared sink, group size exactly 2**: a pair of entries with the
       same union body (same ``.func_arg`` symbol or ``OFFSET(field)``).
       Avoids false positives on shared dispatcher handlers like
       ``opt_old2new`` (used by ``-atag``/``-stag``/``-absf``/...) and on
       stream-type variant families like ``-acodec``/``-vcodec``/``-scodec``
       that share a registry-style storage but are not aliases of each
       other.
    """
    cleaned = "\n".join(_strip_c_comments(text) for text in sources.values())
    entries = _parse_entries(cleaned)
    alt_arrays = _parse_alt_arrays(cleaned)

    by_name: dict[str, _RawEntry] = {}
    for entry in entries:
        by_name.setdefault(entry.name, entry)

    out: dict[str, list[str]] = {}

    def attach(canonical: str, alias: str) -> None:
        if canonical not in documented or alias in documented:
            return
        bucket = out.setdefault(canonical, [])
        if alias not in bucket:
            bucket.append(alias)

    # Signal 1: name_canon — entry self-declares its canonical name.
    for name, entry in by_name.items():
        if entry.canon:
            attach(f"-{entry.canon}", f"-{name}")

    # Signal 2: names_alt — entry declares the names that also trigger it.
    for name, entry in by_name.items():
        if not entry.alt or entry.alt not in alt_arrays:
            continue
        for sibling in alt_arrays[entry.alt]:
            attach(f"-{name}", f"-{sibling}")

    # Signal 2b: standalone name registries (``opt_name_presets[]`` etc.).
    # Older tags (pre-n7) predate the ``.u1.names_alt`` field — the same
    # information lives in ``static const char *const opt_name_X[]`` arrays
    # consumed by registry-style lookup macros. Treat each array as an
    # alias group: when exactly one element is documented, the others are
    # attached as its aliases. Restricted to ``opt_name_`` and ``alt_`` to
    # avoid sweeping in unrelated string tables, and to the
    # exactly-one-documented case to avoid stream-type variant families
    # (``opt_name_codec_tags = {tag, atag, vtag, stag}``) where multiple
    # entries are independently documented but ``stag`` is the lone
    # orphan — aliasing it to any of the others would be misleading.
    for arr_name, members in alt_arrays.items():
        if not (arr_name.startswith("opt_name_") or arr_name.startswith("alt_")):
            continue
        documented_members = [m for m in members if f"-{m}" in documented]
        if len(documented_members) != 1:
            continue
        canonical = documented_members[0]
        for m in members:
            if m == canonical:
                continue
            attach(f"-{canonical}", f"-{m}")

    # Signal 3: shared sink, exactly 2 entries. Pairs only — a larger
    # group nearly always means a generic dispatcher (false positive).
    by_sink: dict[str, list[str]] = {}
    for name, entry in by_name.items():
        by_sink.setdefault(entry.sink, []).append(name)
    for siblings in by_sink.values():
        if len(siblings) != 2:
            continue
        a, b = siblings
        a_doc = f"-{a}" in documented
        b_doc = f"-{b}" in documented
        if a_doc and not b_doc:
            attach(f"-{a}", f"-{b}")
        elif b_doc and not a_doc:
            attach(f"-{b}", f"-{a}")

    return out


def apply_alias_map(options: list[dict], alias_map: dict[str, list[str]]) -> int:
    """Merge ``alias_map`` into each option's ``aliases`` list in place.

    Returns the number of new aliases added (across all options), so callers
    can log a one-line summary. Existing aliases are preserved and
    deduplicated against the new entries; aliases that collide with a
    top-level documented option are dropped (defensive — :func:`build_alias_map`
    already filters these out).
    """
    by_name = {o["name"]: o for o in options}
    documented = set(by_name)
    added = 0
    for canonical, aliases in alias_map.items():
        opt = by_name.get(canonical)
        if opt is None:
            continue
        existing = list(opt.get("aliases") or [])
        for alias in aliases:
            if alias in existing or alias in documented:
                continue
            existing.append(alias)
            added += 1
        opt["aliases"] = existing
    return added


# Type tokens recognized in the ``flags_text`` region of an OptionDef row.
# The new (n7+) tag scheme uses ``OPT_TYPE_*`` enum values in the dedicated
# type slot; the legacy scheme combined the type with the scope/behavior
# flags (``OPT_STRING``, ``OPT_INT``, ...) in a single bitfield. Both spell
# the same set of types — we recognize either form.
_TYPE_TO_VALUETYPE: dict[str, str] = {
    "OPT_TYPE_STRING": "string",
    "OPT_TYPE_INT": "int",
    "OPT_TYPE_INT64": "int",
    "OPT_TYPE_FLOAT": "float",
    "OPT_TYPE_DOUBLE": "float",
    "OPT_TYPE_BOOL": "none",
    "OPT_TYPE_TIME": "string",
    "OPT_STRING": "string",
    "OPT_INT": "int",
    "OPT_INT64": "int",
    "OPT_FLOAT": "float",
    "OPT_DOUBLE": "float",
    "OPT_BOOL": "none",
    "OPT_TIME": "string",
}


def _infer_value_type(flags_text: str, arg_name: str) -> str:
    """Map a row's flag expression to the SPA's ``valueType`` vocabulary.

    Falls back on the placeholder presence for ``OPT_TYPE_FUNC`` entries
    (which dispatch through a custom handler — the type isn't encoded in
    the row): the documented argument name signals that the option
    consumes a value.
    """
    tokens = set(re.findall(r'\bOPT_[A-Z0-9_]+\b', flags_text))
    for tok in tokens:
        vt = _TYPE_TO_VALUETYPE.get(tok)
        if vt is not None:
            return vt
    # OPT_TYPE_FUNC and legacy entries without a dedicated type token:
    # a 4th-arg placeholder (``"format"``, ``"args"``, ...) means the
    # option takes a value, otherwise it's a bare flag.
    if "HAS_ARG" in tokens or arg_name:
        return "string"
    return "none"


def _infer_scope(flags_text: str) -> str:
    """Map ``OPT_INPUT`` / ``OPT_OUTPUT`` to the SPA scope vocabulary."""
    tokens = set(re.findall(r'\bOPT_[A-Z0-9_]+\b', flags_text))
    if "OPT_INPUT" in tokens:
        return "input"
    if "OPT_OUTPUT" in tokens:
        return "output"
    return "global"


def _has_stream_specifier(flags_text: str) -> bool:
    """``OPT_PERSTREAM`` (n7+) / ``OPT_SPEC`` (legacy) both mark per-stream."""
    tokens = set(re.findall(r'\bOPT_[A-Z0-9_]+\b', flags_text))
    return "OPT_PERSTREAM" in tokens or "OPT_SPEC" in tokens


def _synthesize_signature(
    name: str, arg_name: str, scope: str, per_stream: bool
) -> str:
    """Build a documented-form string mimicking the texi signature shape.

    Example for ``-hwaccel_output_format``::

        -hwaccel_output_format[:stream_specifier] format (input,per-stream)
    """
    parts = [f"-{name}"]
    if per_stream:
        parts[0] += "[:stream_specifier]"
    if arg_name:
        parts.append(arg_name)
    tail = scope
    if per_stream:
        tail += ",per-stream"
    parts.append(f"({tail})")
    return " ".join(parts)


def build_undocumented_options(
    sources: dict[str, str], covered: set[str]
) -> list[dict]:
    """Return synthesized option dicts for C-source rows not covered by docs.

    ``covered`` is the set of dash-prefixed names that already appear in
    the texi-derived options list — either as the canonical name of an
    entry or as one of its aliases (including aliases just attached by
    :func:`apply_alias_map`). Any C row whose ``-name`` is in this set is
    skipped; the doc-derived entry wins.

    Each emitted dict matches the shape produced by the texi parser, so
    callers can simply ``options.extend(build_undocumented_options(...))``
    and re-sort. Description carries the short C help string as a single
    paragraph; ``signature`` is synthesized from the inferred scope and
    the row's argument-name placeholder.

    Rows whose flag expression contains no ``OPT_*`` token are skipped —
    that filters out stray ``{ "key", "value" }`` literals from unrelated
    string tables in the same files.
    """
    cleaned = "\n".join(_strip_c_comments(text) for text in sources.values())
    entries = _parse_entries(cleaned)

    out: list[dict] = []
    emitted: set[str] = set()
    for entry in entries:
        dash_name = f"-{entry.name}"
        if dash_name in covered or dash_name in emitted:
            continue
        if not re.search(r'\bOPT_[A-Z0-9_]+\b', entry.flags_text):
            continue
        # Same-file duplicates appear when an option is wrapped in
        # multiple ``#if`` blocks (e.g. ``"hwaccel_output_format"`` is
        # uniquely defined, but ``"videotoolbox_pixfmt"`` and a handful
        # of others have alternative definitions per configuration). The
        # first one wins, matching the order ``ffmpeg_opt.c`` declares
        # them in.
        emitted.add(dash_name)
        scope = _infer_scope(entry.flags_text)
        per_stream = _has_stream_specifier(entry.flags_text)
        value_type = _infer_value_type(entry.flags_text, entry.arg_name)
        out.append(
            {
                "name": dash_name,
                "aliases": [],
                "scope": scope,
                "valueType": value_type,
                "values": [],
                "requires": [],
                "conflicts": [],
                "description": [entry.help_text] if entry.help_text else [],
                "anchor": "",
                "signature": [
                    _synthesize_signature(
                        entry.name, entry.arg_name, scope, per_stream
                    )
                ],
            }
        )
    return out
