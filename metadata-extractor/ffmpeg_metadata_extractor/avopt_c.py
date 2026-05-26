"""Parser for ``AVOption`` arrays in libavcodec / libavformat C source.

The texi docs describe each option (name, paragraphs, sometimes a list of
valid value tokens) but rarely explain what each enum/flag value *means*.
The C source carries that information as ``AV_OPT_TYPE_CONST`` rows inside
the same ``AVOption`` array, each with a short ``help`` string. This module
extracts those arrays and ties them to a class name (the AVClass'
``class_name``) so the higher-level extractor can overlay value
descriptions onto the texi-derived per-codec / per-format option list.

The parser is deliberately ad-hoc: there is no preprocessor, so we
- strip ``//`` and ``/* */`` comments,
- drop ``#if/#ifdef/#elif/#else/#endif`` lines (keep every branch — same
  posture as the synthetic ``config.texi`` used for doc rendering),
- inline object-like ``#define NAME body`` macros that appear bare inside
  an array literal (the ``COMMON_OPTIONS`` / ``LEGACY_OPTIONS`` pattern).
Function-like macros (``FF_RTP_FLAG_OPTS(...)``) are left unexpanded; the
entries they would produce are simply missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from .models import AVOptionEntry

# Synthetic key under which the global ``avformat_options[]`` array is
# stored in :func:`build_class_options_map`. The array lives in
# ``libavformat/options_table.h`` and has no AVClass binding inside that
# file (the binding lives in ``libavformat/options.c``), so we surface
# it under a sentinel key for the extractor to look up directly.
AVFORMAT_GLOBAL_KEY = "__avformat_options__"
AVCODEC_GLOBAL_KEY = "__avcodec_options__"

_AVFORMAT_GLOBAL_ARRAY = "avformat_options"
_AVCODEC_GLOBAL_ARRAYS = ("avcodec_options", "av_codec_context_options")


# --- public data shapes ----------------------------------------------------


@dataclass(frozen=True)
class ParsedOption:
    """One ``AVOption`` row (the parent of any CONST children)."""

    name: str
    help: str
    type: str  # "INT", "FLAGS", "STRING", "BOOL", "FLOAT", "DOUBLE", ...
    unit: str  # "" when the option has no enum/flag children
    # Each CONST child as (value_name, help). Ordered as they appear in the
    # array. Empty when the option is not enum/flag-typed.
    values: list[tuple[str, str]] = field(default_factory=list)


# --- comment / preprocessor cleanup ----------------------------------------


_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_PREPROC = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif|error|warning|pragma)\b[^\n]*\n?",
    re.MULTILINE,
)


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub("", text)
    return text


def _strip_preproc_conditionals(text: str) -> str:
    return _PREPROC.sub("", text)


# --- macro inlining (object-like only) -------------------------------------

# ``#define NAME body...`` where body may span lines via trailing ``\``.
# Captures: 1=name, 2=raw body (with backslashes still present).
_DEFINE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_(])[ \t]+"
    r"((?:[^\n\\]|\\(?:\r?\n|.))*)\n",
    re.MULTILINE,
)


def _collect_object_macros(text: str) -> dict[str, str]:
    """Return ``{name: body}`` for object-like ``#define`` macros.

    Function-like macros (``NAME(args)``) are ignored — the lookahead
    ``(?![A-Za-z0-9_(])`` after the name skips ``#define NAME(args) ...``.
    Backslash-newline continuations in the body are folded to spaces.
    """
    out: dict[str, str] = {}
    for m in _DEFINE.finditer(text):
        name = m.group(1)
        body = m.group(2)
        body = re.sub(r"\\\r?\n", " ", body).strip()
        if not body:
            continue
        out[name] = body
    return out


def _inline_macros_in_array_body(body: str, macros: dict[str, str]) -> str:
    """Replace bare ``MACRO_NAME`` tokens (no following ``(``) inside an
    array body with their object-like ``#define`` bodies.

    Only substitutes identifiers that are entirely upper-case + digits +
    underscores — keeps us from accidentally replacing struct field names
    or ``AV_OPT_TYPE_INT`` tokens.
    """
    if not macros:
        return body

    pattern = re.compile(r"\b([A-Z_][A-Z0-9_]{2,})\b(?!\s*\()")

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        return macros.get(name, name)

    # Two passes — a macro body can reference another object-like macro.
    body = pattern.sub(repl, body)
    body = pattern.sub(repl, body)
    return body


# --- AVOption array discovery + tokenization -------------------------------


# ``static const? AVOption NAME [ ... ] = ``
_ARRAY_HEAD = re.compile(
    r"\bstatic\s+(?:const\s+)?AVOption\s+([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*\]\s*=\s*\{",
)


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Given ``text[open_idx] == '{'``, return the index of the matching
    ``}``. Honors string literals and char literals so braces inside them
    don't disturb the count. Returns ``-1`` if unmatched.
    """
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            # Skip string literal.
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


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    """Split ``body`` on ``sep`` ignoring separators inside braces,
    parentheses, brackets, and string/char literals."""
    parts: list[str] = []
    buf: list[str] = []
    depth_brace = depth_paren = depth_brack = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n and body[i] != '"':
                if body[i] == "\\" and i + 1 < n:
                    buf.append(body[i])
                    buf.append(body[i + 1])
                    i += 2
                    continue
                buf.append(body[i])
                i += 1
            if i < n:
                buf.append(body[i])
                i += 1
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n and body[i] != "'":
                if body[i] == "\\" and i + 1 < n:
                    buf.append(body[i])
                    buf.append(body[i + 1])
                    i += 2
                    continue
                buf.append(body[i])
                i += 1
            if i < n:
                buf.append(body[i])
                i += 1
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack -= 1
        elif (
            ch == sep
            and depth_brace == 0
            and depth_paren == 0
            and depth_brack == 0
        ):
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


_STRING_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"')


def _coalesce_strings(text: str) -> str | None:
    """Take an entry-field slice and return the concatenation of any
    adjacent string literals (C-style implicit concat). Returns ``None``
    when the slice has no string literal (e.g. it is ``NULL`` or ``0``).
    """
    parts = _STRING_LITERAL.findall(text)
    if not parts:
        return None
    out: list[str] = []
    for p in parts:
        # Decode a small subset of C escapes that show up in help strings.
        decoded = (
            p.replace('\\"', '"')
            .replace("\\\\", "\\")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
        out.append(decoded)
    return "".join(out)


_TYPE_TOKEN = re.compile(r"\bAV_OPT_TYPE_([A-Z0-9_]+)\b")
# Legacy ``FF_OPT_TYPE_*`` (pre-2014 FFmpeg) — same suffix space, normalize
# to the modern spelling.
_LEGACY_TYPE_TOKEN = re.compile(r"\bFF_OPT_TYPE_([A-Z0-9_]+)\b")
_UNIT_FIELD = re.compile(r'\.unit\s*=\s*"((?:\\.|[^"\\])*)"')


def _parse_entry(entry_src: str) -> tuple[str, str, str, str] | None:
    """Parse one ``{ ... }`` entry into ``(name, help, type, unit)``.

    Returns ``None`` for the array-terminating ``{ NULL }`` entry or
    anything that doesn't look like a valid option row (no type tag).
    """
    inner = entry_src.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    if not inner:
        return None

    # First positional: the option name. Must be a string literal — bail
    # on ``{ NULL }`` sentinels and the like.
    fields = _split_top_level(inner)
    if not fields:
        return None
    name = _coalesce_strings(fields[0])
    if name is None:
        return None

    help_text = ""
    if len(fields) >= 2:
        h = _coalesce_strings(fields[1])
        if h is not None:
            help_text = h

    type_match = _TYPE_TOKEN.search(inner) or _LEGACY_TYPE_TOKEN.search(inner)
    if not type_match:
        return None
    type_name = type_match.group(1)

    unit = ""
    um = _UNIT_FIELD.search(inner)
    if um:
        unit = um.group(1)

    return (name, help_text, type_name, unit)


def _parse_array_body(body: str) -> list[ParsedOption]:
    """Turn a raw array literal body into parented :class:`ParsedOption`
    entries. CONST rows are folded into their parent's ``values`` list,
    keyed by ``.unit``. CONST rows whose unit has no matching parent (rare
    — happens when the parent was eliminated by a preprocessor branch we
    couldn't model) are dropped silently.
    """
    raw_entries: list[tuple[str, str, str, str]] = []
    for chunk in _split_top_level(body):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Tolerate trailing comments / stray semicolons.
        if not chunk.startswith("{"):
            continue
        # Find the matching close brace inside this entry. ``_split_top_level``
        # already balanced braces, so the chunk is a single ``{...}``.
        end = _find_matching_brace(chunk, 0)
        if end == -1:
            continue
        entry_src = chunk[: end + 1]
        parsed = _parse_entry(entry_src)
        if parsed is not None:
            raw_entries.append(parsed)

    # Build parent options in order; remember the latest parent for each
    # unit so we can attach CONST children.
    parents: list[ParsedOption] = []
    parent_by_unit: dict[str, ParsedOption] = {}
    for name, help_text, type_name, unit in raw_entries:
        if type_name == "CONST":
            target = parent_by_unit.get(unit) if unit else None
            if target is None:
                continue
            target.values.append((name, help_text))
            continue
        opt = ParsedOption(
            name=name, help=help_text, type=type_name, unit=unit, values=[]
        )
        parents.append(opt)
        if unit:
            parent_by_unit[unit] = opt
    return parents


# --- AVClass binding -------------------------------------------------------


_AVCLASS_HEAD = re.compile(
    r"\bstatic\s+(?:const\s+)?AVClass\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{",
)
_CLASS_NAME_FIELD = re.compile(r'\.class_name\s*=\s*"((?:\\.|[^"\\])*)"')
_OPTION_FIELD = re.compile(r"\.option\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\b")

_ROLE_SUFFIXES = (" encoder", " decoder", " muxer", " demuxer", " filter",
                  " protocol", " bitstream filter")


def _normalize_class_name(raw: str) -> list[str]:
    """Strip role suffixes and split slash-aliased class names.

    ``"libvpx-vp8 encoder"`` -> ``["libvpx-vp8"]``
    ``"mov/mp4/tgp/psp/tg2/ipod/ismv/f4v muxer"``
        -> ``["mov", "mp4", "tgp", "psp", "tg2", "ipod", "ismv", "f4v"]``
    """
    name = raw.strip()
    lower = name.lower()
    for suffix in _ROLE_SUFFIXES:
        if lower.endswith(suffix):
            name = name[: -len(suffix)].rstrip()
            break
    parts = [p.strip() for p in name.split("/") if p.strip()]
    return parts


# --- top-level driver ------------------------------------------------------


def _parse_file_arrays(text: str) -> dict[str, list[ParsedOption]]:
    """Return ``{c_symbol_name: [options]}`` for every ``AVOption`` array
    declared in ``text``. Used by both :func:`parse_c_file` (which then
    resolves the class binding) and :func:`build_class_options_map`
    (which pulls global tables directly by symbol name).
    """
    text = _strip_comments(text)
    macros = _collect_object_macros(text)
    text_no_pp = _strip_preproc_conditionals(text)

    arrays: dict[str, list[ParsedOption]] = {}
    for m in _ARRAY_HEAD.finditer(text_no_pp):
        array_name = m.group(1)
        open_idx = text_no_pp.find("{", m.end() - 1)
        if open_idx == -1:
            continue
        close_idx = _find_matching_brace(text_no_pp, open_idx)
        if close_idx == -1:
            continue
        body = text_no_pp[open_idx + 1 : close_idx]
        body = _inline_macros_in_array_body(body, macros)
        arrays[array_name] = _parse_array_body(body)
    return arrays


def parse_c_file(text: str) -> dict[str, list[ParsedOption]]:
    """Parse one C source and return ``{class_name: [options]}``.

    Each ``class_name`` is normalized via :func:`_normalize_class_name`,
    so a single AVClass with a slash-aliased name (e.g. the mov muxer)
    contributes the same option list under several keys.
    """
    # We need ``text`` stripped twice — once for array discovery (handled
    # in ``_parse_file_arrays``) and once for AVClass discovery. Run the
    # same cleanup here so brace matching aligns with the AVClass regex.
    arrays = _parse_file_arrays(text)
    text_no_pp = _strip_preproc_conditionals(_strip_comments(text))

    out: dict[str, list[ParsedOption]] = {}
    for m in _AVCLASS_HEAD.finditer(text_no_pp):
        open_idx = text_no_pp.find("{", m.end() - 1)
        if open_idx == -1:
            continue
        close_idx = _find_matching_brace(text_no_pp, open_idx)
        if close_idx == -1:
            continue
        class_body = text_no_pp[open_idx + 1 : close_idx]
        name_match = _CLASS_NAME_FIELD.search(class_body)
        option_match = _OPTION_FIELD.search(class_body)
        if not name_match or not option_match:
            continue
        array_name = option_match.group(1)
        options = arrays.get(array_name)
        if not options:
            continue
        for plain in _normalize_class_name(name_match.group(1)):
            out.setdefault(plain.lower(), options)
    return out


# --- Tree-level driver -----------------------------------------------------


_C_FILE_EXTS = (".c", ".h")


def build_class_options_map(
    libav_roots: tuple[Path, ...],
) -> dict[str, list[ParsedOption]]:
    """Walk every ``.c``/``.h`` file under each root and return a unified
    ``{key: [options]}`` map.

    Keys are AVClass names (normalized to lowercase), plus the synthetic
    :data:`AVFORMAT_GLOBAL_KEY` / :data:`AVCODEC_GLOBAL_KEY` for the
    global option tables (``avformat_options[]`` /
    ``avcodec_options[]``), which have no AVClass binding inside their
    own source file.

    Missing roots are skipped silently — the staging step is best-effort
    and older tags reshuffled paths.
    """
    out: dict[str, list[ParsedOption]] = {}
    for root in libav_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in _C_FILE_EXTS or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Global tables — keyed under sentinel names so the extractor
            # can fetch them directly without parsing the AVClass wiring
            # (which lives in a different file).
            arrays = _parse_file_arrays(text)
            if _AVFORMAT_GLOBAL_ARRAY in arrays:
                out.setdefault(AVFORMAT_GLOBAL_KEY, arrays[_AVFORMAT_GLOBAL_ARRAY])
            for arr_name in _AVCODEC_GLOBAL_ARRAYS:
                if arr_name in arrays:
                    out.setdefault(AVCODEC_GLOBAL_KEY, arrays[arr_name])
                    break

            # Per-class option sets. A later file's class binding wins
            # only if no earlier file already registered the same key,
            # to keep results stable across stable file orderings.
            for key, options in parse_c_file(text).items():
                out.setdefault(key, options)
    return out


# --- Overlay onto texi-derived AVOptionEntry ------------------------------


def _collect_c_options_for_keys(
    c_map: dict[str, list[ParsedOption]],
    keys: Iterable[str],
) -> dict[str, ParsedOption]:
    """Walk the given class keys (canonical name + aliases) and return
    ``{option_name: ParsedOption}``, with the first occurrence winning.

    Keys are case-folded for the lookup; falsy keys are skipped.
    """
    by_option: dict[str, ParsedOption] = {}
    for key in keys:
        if not key:
            continue
        for opt in c_map.get(key.lower(), []):
            by_option.setdefault(opt.name, opt)
    return by_option


def enrich_options_with_c_values(
    options: list[AVOptionEntry],
    c_map: dict[str, list[ParsedOption]],
    keys: Iterable[str],
) -> list[AVOptionEntry]:
    """Return a new list of options with ``values``/``value_descriptions``
    enriched from the C-source data.

    For each existing texi-derived option, look up the matching C-source
    option (by stripping the leading ``-`` from the texi name and matching
    on any of ``keys``). When found:

    - Keep the texi-derived ``values`` ordering, append C-source values
      the texi didn't know about.
    - Pair each value with the C-source ``help`` string in
      ``value_descriptions`` (same length as ``values``, "" when unknown).

    C-source-only options (those texi never documented) are appended to
    the result; they get an empty description list, no aliases / anchor /
    signature, and ``value_type`` derived from the C type tag.
    """
    if not options and not c_map:
        return list(options)

    by_option = _collect_c_options_for_keys(c_map, keys)
    seen_names: set[str] = set()
    out: list[AVOptionEntry] = []

    for entry in options:
        bare = entry.name[1:] if entry.name.startswith("-") else entry.name
        seen_names.add(bare)
        c_opt = by_option.get(bare)
        if c_opt is None:
            # Keep existing entry, ensure value_descriptions is aligned.
            if len(entry.value_descriptions) != len(entry.values):
                entry = replace(
                    entry,
                    value_descriptions=[""] * len(entry.values),
                )
            out.append(entry)
            continue

        # Merge values: texi-first, then C-source extras.
        merged_values: list[str] = list(entry.values)
        merged_descs: list[str] = list(entry.value_descriptions)
        # Pad descs to match values length first (texi might have left it empty).
        if len(merged_descs) < len(merged_values):
            merged_descs.extend([""] * (len(merged_values) - len(merged_descs)))

        c_help_by_name = {n: h for n, h in c_opt.values}
        # Fill descriptions for texi-known values.
        for i, vname in enumerate(merged_values):
            if not merged_descs[i] and vname in c_help_by_name:
                merged_descs[i] = c_help_by_name[vname]
        # Append C-source-only values.
        for vname, vhelp in c_opt.values:
            if vname not in merged_values:
                merged_values.append(vname)
                merged_descs.append(vhelp)

        out.append(
            replace(
                entry,
                values=merged_values,
                value_descriptions=merged_descs,
            )
        )

    # Append C-source-only options (texi missed them entirely).
    for bare, c_opt in by_option.items():
        if bare in seen_names:
            continue
        out.append(
            AVOptionEntry(
                name=f"-{bare}",
                aliases=[],
                value_type=_value_type_from_c_type(c_opt.type),
                values=[n for n, _ in c_opt.values],
                description=[c_opt.help] if c_opt.help else [],
                anchor="",
                signature=[],
                roles=[],
                value_descriptions=[h for _, h in c_opt.values],
            )
        )

    return out


_C_TYPE_MAP = {
    "INT": "int",
    "INT64": "int",
    "UINT": "int",
    "UINT64": "int",
    "FLOAT": "float",
    "DOUBLE": "float",
    "BOOL": "bool",
    "STRING": "string",
    "FLAGS": "flags",
    "DURATION": "duration",
    "DICT": "dict",
    "BINARY": "binary",
    "RATIONAL": "rational",
    "IMAGE_SIZE": "image_size",
    "PIXEL_FMT": "pixel_format",
    "SAMPLE_FMT": "sample_format",
    "VIDEO_RATE": "video_rate",
    "COLOR": "color",
    "CHANNEL_LAYOUT": "channel_layout",
    "CHLAYOUT": "channel_layout",
    "CONST": "none",  # shouldn't appear as a top-level entry, but be safe
}


def _value_type_from_c_type(c_type: str) -> str:
    """Map an ``AV_OPT_TYPE_*`` suffix to the schema's value_type token."""
    return _C_TYPE_MAP.get(c_type, "string")
