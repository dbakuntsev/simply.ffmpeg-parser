"""Parse undocumented option aliases from FFmpeg's C source.

FFmpeg's ``OptionDef options[]`` table in ``fftools/ffmpeg_opt.c`` (and the
``CMDUTILS_COMMON_OPTIONS`` macro in ``fftools/opt_common.h`` /
``fftools/cmdutils.h``) defines several short/legacy alternative names that
share a single backing handler with a "canonical" option but aren't always
reflected in the texi docs the rest of the extractor reads. Common cases that
exist in the source but not the doc-parsed ``options.json``:

- ``apre`` / ``vpre`` / ``spre`` → alias of ``pre``    (via ``name_canon``)
- ``stag``                       → alias of ``tag``    (via ``name_canon``)
- ``scodec`` / ``dcodec``        → alias of ``codec``  (via ``names_alt``)
- ``lavfi``                      → alias of ``filter_complex`` (in older
  tags, where ``-lavfi`` lacks its own doc entry; same ``.func_arg``)

This module walks ``fftools/ffmpeg_opt.c`` (and the common-options macro file)
to recover those links and exposes them as a
``{canonical_dash_name: [alias_dash_name, ...]}`` map that
:func:`apply_alias_map` folds into each documented option's ``aliases`` list.
Only undocumented siblings are added — documented options keep their own
top-level entries, so no doc-derived metadata is overwritten.
"""

from __future__ import annotations

import re


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


def _parse_entries(text: str) -> list[tuple[str, str, str | None, str | None]]:
    """Walk every ``{ "name", flags, { sink } [, .u1.* ] }`` entry.

    Returns ``(name, sink_signature, name_canon, alt_array_ref)`` tuples.
    ``sink_signature`` is the union body (the second ``{...}``) with
    whitespace stripped — sufficient to detect entries that share a backing
    handler (same ``.func_arg`` symbol or same ``OFFSET(field)``).

    Entries without a parseable union body are skipped silently — the C
    source includes macro lines (``CMDUTILS_COMMON_OPTIONS``) and stray
    string-pairs in unrelated tables that this parser shouldn't fail over.
    """
    out: list[tuple[str, str, str | None, str | None]] = []
    n = len(text)
    for m in _ENTRY_NAME_RE.finditer(text):
        name = m.group(1)
        i = m.end()
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
        out.append(
            (
                name,
                sink,
                canon_m.group(1) if canon_m else None,
                alt_m.group(1) if alt_m else None,
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

    by_name: dict[str, tuple[str, str | None, str | None]] = {}
    for name, sink, canon, alt in entries:
        by_name.setdefault(name, (sink, canon, alt))

    out: dict[str, list[str]] = {}

    def attach(canonical: str, alias: str) -> None:
        if canonical not in documented or alias in documented:
            return
        bucket = out.setdefault(canonical, [])
        if alias not in bucket:
            bucket.append(alias)

    # Signal 1: name_canon — entry self-declares its canonical name.
    for name, (_sink, canon, _alt) in by_name.items():
        if canon:
            attach(f"-{canon}", f"-{name}")

    # Signal 2: names_alt — entry declares the names that also trigger it.
    for name, (_sink, _canon, alt) in by_name.items():
        if not alt or alt not in alt_arrays:
            continue
        for sibling in alt_arrays[alt]:
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
    for name, (sink, _canon, _alt) in by_name.items():
        by_sink.setdefault(sink, []).append(name)
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
