from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ExtractConfig:
    repo: Path
    out: Path
    tags: list[str] | None
    tag_range: tuple[str, str] | None
    latest_per_minor: bool
    categories: set[str]
    verbose: bool
    continue_on_error: bool
    worktree_fallback: bool
    html_doc: bool


@dataclass(frozen=True)
class OptionEntry:
    name: str
    aliases: list[str]
    scope: str
    value_type: str
    values: list[str]
    requires: list[str]
    conflicts: list[str]
    description: list[str]
    # HTML anchor used by ``ffmpeg-all.html``. Set to the explicit ``@anchor{}``
    # immediately preceding this option's ``@item`` when present (e.g.
    # ``filter_005foption`` for ``-filter``), otherwise the enclosing section's
    # anchor (e.g. ``Main-options`` for most options under ``@section Main
    # options``). Empty when no enclosing section could be resolved.
    anchor: str = ""
    # The documented invocation form, with @var/@emph markup stripped — e.g.
    # ``-map [-]input_file_id[:stream_specifier][:view_specifier][:?] |
    # [linklabel] (output)`` for ``@item -map [-]@var{input_file_id}...``.
    # One entry per ``@item``/``@itemx`` line: most options have one signature
    # but some declare aliases with different forms. Empty list means the
    # option came from a path with no texi source (no parseable @item).
    signature: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodecEntry:
    name: str
    type: str
    aliases: list[str]
    encoder: bool
    decoder: bool
    # HTML anchor for the codec's section in ``ffmpeg-all.html``. Falls back to
    # the makeinfo-encoded section title when no explicit ``@anchor{}`` is
    # present — captures multi-name forms like ``libx264, libx264rgb`` ⇒
    # ``libx264_002c-libx264rgb``. Empty for entries that only exist in
    # ``allcodecs.c`` (no documentation section).
    anchor: str = ""


@dataclass(frozen=True)
class FilterEntry:
    name: str
    type: str
    aliases: list[str]
    params: list[str]
    description: list[str]
    args: dict[str, list[str]]


@dataclass(frozen=True)
class NamedEntry:
    """An entry produced from a `@section` (or `@item` inside a grouped section)
    of a single-entity catalog texi: demuxers, muxers, protocols, bitstream
    filters. The shape is intentionally simple — the SPA uses these for
    value-level enrichment of options like `-f`, `-bsf`, and protocol URIs.

    ``anchor`` is the explicit ``@anchor{...}`` value (or the section title)
    used by makeinfo for the HTML id. The SPA combines it with
    ``ffmpeg-all.html#<anchor>`` to produce a deep link.
    """

    name: str
    aliases: list[str]
    anchor: str
    description: list[str]
