"""First-seen-wins dedupe helpers shared by every parser variant.

Each function returns a list sorted by canonical name so the resulting
JSON is byte-stable across re-extractions. Options/AVOptions additionally
track claimed alias names so a later, weaker entry sharing a name with
an existing primary's alias is silently dropped (keeps the aliases list
unambiguously single-valued).
"""

from __future__ import annotations

from .models import AVOptionEntry, CodecEntry, FilterEntry, NamedEntry, OptionEntry


def dedupe_options(options: list[OptionEntry]) -> list[OptionEntry]:
    seen: dict[str, OptionEntry] = {}
    claimed_aliases: set[str] = set()
    for option in options:
        if option.name in seen or option.name in claimed_aliases:
            # A later, weaker entry sharing a name with an existing primary
            # (e.g. ``-v`` appearing both as alias of ``-loglevel`` and as a
            # stub item somewhere else) is dropped to keep aliases unique.
            continue
        seen[option.name] = option
        for alias in option.aliases:
            claimed_aliases.add(alias)
    return sorted(seen.values(), key=lambda o: o.name)


def dedupe_av_options(options: list[AVOptionEntry]) -> list[AVOptionEntry]:
    seen: dict[str, AVOptionEntry] = {}
    claimed_aliases: set[str] = set()
    for option in options:
        if option.name in seen or option.name in claimed_aliases:
            continue
        seen[option.name] = option
        for alias in option.aliases:
            claimed_aliases.add(alias)
    return sorted(seen.values(), key=lambda o: o.name)


def dedupe_codecs(codecs: list[CodecEntry]) -> list[CodecEntry]:
    seen: dict[str, CodecEntry] = {}
    for codec in codecs:
        if codec.name not in seen:
            seen[codec.name] = codec
    return sorted(seen.values(), key=lambda c: c.name)


def dedupe_filters(filters: list[FilterEntry]) -> list[FilterEntry]:
    seen: dict[str, FilterEntry] = {}
    for flt in filters:
        if flt.name not in seen:
            seen[flt.name] = flt
    return sorted(seen.values(), key=lambda f: f.name)


def dedupe_named(entries: list[NamedEntry]) -> list[NamedEntry]:
    seen: dict[str, NamedEntry] = {}
    for entry in entries:
        if entry.name not in seen:
            seen[entry.name] = entry
    return sorted(seen.values(), key=lambda e: e.name)
