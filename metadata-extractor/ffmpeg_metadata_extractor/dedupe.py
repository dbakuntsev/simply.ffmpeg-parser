"""First-seen-wins dedupe by ``.name`` shared by every parser variant.

A single generic :func:`dedupe_by_name` does the work; the five historical
names (``dedupe_options``, ``dedupe_av_options``, ``dedupe_codecs``,
``dedupe_filters``, ``dedupe_named``) survive as one-line aliases so the
:mod:`.parsing` facade and any external caller keep working.

Each returned list is sorted by canonical name so the resulting JSON is
byte-stable across re-extractions. ``claim_aliases=True`` additionally
drops later entries whose ``.name`` was claimed as an alias by an
earlier-kept entry — used for options and AVOptions where alias
uniqueness matters (e.g. ``-v`` declared both as alias of ``-loglevel``
and as a stub item somewhere else: the stub is dropped).
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from .models import AVOptionEntry, CodecEntry, FilterEntry, NamedEntry, OptionEntry


class _NamedWithAliases(Protocol):
    """Structural type for any model the dedupe helpers consume — always
    has ``.name``, and ``.aliases`` is present on models that use the
    ``claim_aliases`` mode."""

    name: str
    aliases: list[str]


T = TypeVar("T", bound=_NamedWithAliases)


def dedupe_by_name(items: list[T], *, claim_aliases: bool = False) -> list[T]:
    """First-seen-wins dedupe by ``.name``, returned sorted by name.

    With ``claim_aliases=True``, also drop later entries whose ``.name``
    matches an alias claimed by an earlier-kept entry's ``.aliases`` list.
    Models without an ``.aliases`` attribute should be passed without the
    flag (the codec / filter / named-entry variants take this path).
    """
    seen: dict[str, T] = {}
    claimed_aliases: set[str] = set()
    for item in items:
        if item.name in seen or item.name in claimed_aliases:
            continue
        seen[item.name] = item
        if claim_aliases:
            for alias in item.aliases:
                claimed_aliases.add(alias)
    return sorted(seen.values(), key=lambda x: x.name)


# --- Public-API aliases ---------------------------------------------------
#
# Each historical name is a thin specialization of :func:`dedupe_by_name`.
# Kept as separate functions (rather than ``dedupe_options = ...``) so
# callers see meaningful names in tracebacks and the ``parsing`` facade
# can re-export them unchanged.


def dedupe_options(options: list[OptionEntry]) -> list[OptionEntry]:
    return dedupe_by_name(options, claim_aliases=True)


def dedupe_av_options(options: list[AVOptionEntry]) -> list[AVOptionEntry]:
    return dedupe_by_name(options, claim_aliases=True)


def dedupe_codecs(codecs: list[CodecEntry]) -> list[CodecEntry]:
    return dedupe_by_name(codecs)


def dedupe_filters(filters: list[FilterEntry]) -> list[FilterEntry]:
    return dedupe_by_name(filters)


def dedupe_named(entries: list[NamedEntry]) -> list[NamedEntry]:
    return dedupe_by_name(entries)
