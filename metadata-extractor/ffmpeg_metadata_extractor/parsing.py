"""Backwards-compatibility facade for the parser layer.

Historically every parser lived in this single 1700-line module. It was
split for readability — the implementations now live in focused modules
beside it:

- :mod:`.texi_markdown` — Markdown rendering of ``makeinfo --xml`` output.
- :mod:`.texi_traversal` — section / anchor utilities.
- :mod:`.options_parser` — driver options (``parse_options_xml``).
- :mod:`.av_options_parser` — generic + per-codec / per-format AVOptions.
- :mod:`.codecs_parser` — codec catalog from texi + ``allcodecs.c``.
- :mod:`.filters_parser` — filter catalog from ``filters.texi``.
- :mod:`.named_parser` — demuxers / muxers / protocols / bitstream
  filters / input devices / output devices.
- :mod:`.dedupe` — first-seen-wins dedupe helpers.

New code should import directly from those modules; this file only
re-exports the historical public surface so any caller still importing
``from .parsing import parse_X_xml`` keeps working.
"""

from __future__ import annotations

from .av_options_parser import (
    merge_per_codec_options,
    parse_codec_options_xml,
    parse_format_options_xml,
    parse_per_codec_options_xml,
    parse_per_format_options_xml,
)
from .codecs_parser import merge_codec_flags, parse_codecs_c, parse_codecs_xml
from .dedupe import (
    dedupe_av_options,
    dedupe_codecs,
    dedupe_filters,
    dedupe_named,
    dedupe_options,
)
from .filters_parser import parse_filters_xml
from .named_parser import (
    parse_bitstream_filters_xml,
    parse_demuxers_xml,
    parse_input_devices_xml,
    parse_muxers_xml,
    parse_output_devices_xml,
    parse_protocols_xml,
)
from .options_parser import parse_options_xml

__all__ = [
    "dedupe_av_options",
    "dedupe_codecs",
    "dedupe_filters",
    "dedupe_named",
    "dedupe_options",
    "merge_codec_flags",
    "merge_per_codec_options",
    "parse_bitstream_filters_xml",
    "parse_codec_options_xml",
    "parse_codecs_c",
    "parse_codecs_xml",
    "parse_demuxers_xml",
    "parse_filters_xml",
    "parse_format_options_xml",
    "parse_input_devices_xml",
    "parse_muxers_xml",
    "parse_options_xml",
    "parse_output_devices_xml",
    "parse_per_codec_options_xml",
    "parse_per_format_options_xml",
    "parse_protocols_xml",
]
