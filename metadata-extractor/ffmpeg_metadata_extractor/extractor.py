"""Top-level orchestration for the per-tag FFmpeg metadata extraction.

This module wires together the focused modules:

- :mod:`.tag_selection` — discover and filter the tags to process.
- :mod:`.staging` — materialize each tag's ``doc/`` (+ libav* sources).
- :mod:`._logging` — per-tag logger with cross-process queue/drain.
- :mod:`.upstream_enrich` — x264/x265 snapshot resolution and parsing.
- :mod:`.attribution` — third-party license-text fetch and notices page.
- :mod:`.html_doc` — ``makeinfo --html`` rendering of ``ffmpeg-all.html``.

Per-tag work happens in :func:`_extract_for_tag` (called once per tag —
sequentially or via a process pool). Each category is one ``_extract_*``
function reading from a memoized makeinfo-XML cache; results are serialized
to ``<out>/metadata/ffmpeg/<major.minor>/<category>.json``.

The only public surface (used by :mod:`.cli`) is :func:`run_extraction`
and :class:`ExtractConfig` (re-exported from :mod:`.models`).
"""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import threading
from pathlib import Path
from xml.etree import ElementTree as ET

from ._logging import Logger, drain_log_queue, set_log_queue
from .attribution import (
    ensure_license_texts,
    generate_notices_page,
    set_upstream_doc_lock,
)
from .avopt_c import (
    AVCODEC_GLOBAL_KEY,
    AVFORMAT_GLOBAL_KEY,
    ParsedOption,
    build_class_options_map,
    enrich_options_with_c_values,
)
from .git_utils import show_file, tag_date_iso, temporary_worktree
from .html_doc import generate_html_doc
from .models import AVOptionEntry, ExtractConfig, ExtractionError
from .options_c import (
    apply_alias_map,
    build_alias_map,
    build_undocumented_options,
)
from .parsing import (
    dedupe_av_options,
    dedupe_codecs,
    dedupe_filters,
    dedupe_named,
    dedupe_options,
    merge_codec_flags,
    merge_per_codec_options,
    parse_bitstream_filters_xml,
    parse_codec_options_xml,
    parse_codecs_c,
    parse_codecs_xml,
    parse_demuxers_xml,
    parse_filters_xml,
    parse_format_options_xml,
    parse_input_devices_xml,
    parse_muxers_xml,
    parse_output_devices_xml,
    parse_options_xml,
    parse_per_codec_options_xml,
    parse_per_format_options_xml,
    parse_protocols_xml,
)
from .staging import staged_doc
from .tag_selection import parse_tag_version, select_tags
from .texi_xml import MakeinfoError, resolve_makeinfo, run_makeinfo
from .upstream_enrich import (
    X264_FAMILY,
    X265_FAMILY,
    enrich_x264,
    enrich_x265,
    layer_upstream_string_values,
)
from .upstream_help import UpstreamOptionHelp

__all__ = ["ExtractConfig", "ExtractionError", "run_extraction"]


def _pool_initializer(log_queue, upstream_doc_lock) -> None:
    """Runs once per worker process at pool start-up. Forwards the shared
    log queue to :mod:`._logging` and the upstream-doc lock to
    :mod:`.attribution` so every Logger emit and every upstream-doc write
    is properly serialized."""
    set_log_queue(log_queue)
    set_upstream_doc_lock(upstream_doc_lock)


def _extract_for_tag_pooled(
    config: ExtractConfig, tag: str, makeinfo_cmd: list[str]
) -> bool:
    """Worker entry point for ``--jobs > 1`` extraction.

    The worker ships its log lines to the parent's drain thread via the queue
    set up in :func:`_pool_initializer`, so users see streaming progress while
    a single process remains the sole console writer. Returns ``True`` on
    success, ``False`` after warning about the failure — the parent only needs
    the boolean to track failures.
    """
    logger = Logger(config.verbose, tag=tag)
    try:
        _extract_for_tag(config, tag, makeinfo_cmd, logger)
        return True
    except ExtractionError as exc:
        logger.warn(f"Extraction failed: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — surface every failure mode
        logger.warn(f"Worker crashed: {type(exc).__name__}: {exc}")
        return False


def _load_xml(
    doc_root: Path,
    filename: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    cache: dict[str, ET.Element | None] | None = None,
) -> ET.Element | None:
    """Render ``doc/<filename>`` via ``makeinfo --xml`` and return the root.

    A ``cache`` dict (per-tag) memoizes results so the same ``.texi`` file
    isn't re-rendered when multiple extraction steps consume it (e.g.
    ``codecs.texi`` is read for both ``codecs`` and ``codec_options``;
    ``muxers.texi`` / ``demuxers.texi`` are each read twice as well). The
    cache stores ``None`` for missing/unparseable sources so a second
    lookup doesn't re-emit the warning.
    """
    if cache is not None and filename in cache:
        return cache[filename]
    src = doc_root / "doc" / filename
    if not src.exists():
        result: ET.Element | None = None
    else:
        try:
            result = run_makeinfo(src, cwd=src.parent, cmd=makeinfo_cmd)
        except MakeinfoError as exc:
            logger.warn(f"makeinfo failed on {filename}: {exc}")
            result = None
    if cache is not None:
        cache[filename] = result
    return result


def _load_text(repo: Path, tag: str, path: str, fallback_root: Path | None) -> str | None:
    content = show_file(repo, tag, path)
    if content is not None:
        return content
    if fallback_root is None:
        return None
    file_path = fallback_root / path
    if file_path.exists():
        return file_path.read_text(encoding="utf-8", errors="ignore")
    return None


def _extract_options(
    doc_root: Path,
    repo: Path,
    tag: str,
    fallback_root: Path | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    for source in ("ffmpeg.texi", "ffmpeg-all.texi", "ffmpeg-opt.texi"):
        root = _load_xml(doc_root, source, makeinfo_cmd, logger, xml_cache)
        if root is None:
            continue
        logger.debug(f"Parsing options from {source}")
        options = dedupe_options(parse_options_xml(root))
        out: list[dict] = [
            {
                "name": o.name,
                "aliases": o.aliases,
                "scope": o.scope,
                "valueType": o.value_type,
                "values": o.values,
                "requires": o.requires,
                "conflicts": o.conflicts,
                "description": o.description,
                "anchor": o.anchor,
                "signature": o.signature,
            }
            for o in options
        ]
        _augment_options_from_c(out, repo, tag, fallback_root, logger)
        return out
    raise ExtractionError("Options sources not found")


# Files searched (in order) for the ``OptionDef`` table and the
# ``CMDUTILS_COMMON_OPTIONS`` macro. Both names of the common-options header
# appear across the supported tag range: ``opt_common.h`` is the current
# location; ``cmdutils.h`` was its home in older tags (pre-n6 roughly). All
# are best-effort — missing files just contribute nothing to the alias map.
_OPTIONS_C_SOURCES = (
    "fftools/ffmpeg_opt.c",
    "fftools/opt_common.h",
    "fftools/cmdutils.h",
)


def _augment_options_from_c(
    options: list[dict],
    repo: Path,
    tag: str,
    fallback_root: Path | None,
    logger: Logger,
) -> None:
    """Fold information from ``fftools/`` C sources into ``options`` in place.

    Two passes, both gap-fill only — documented entries are never
    overwritten:

    1. **Aliases**: short/legacy alternatives that share a backing
       handler with a documented option (e.g. ``-apre``/``-vpre``/``-spre``
       for ``-pre``, ``-stag`` for ``-tag``, ``-lavfi`` for
       ``-filter_complex`` on older tags) are attached to the documented
       canonical's ``aliases`` list.

    2. **Top-level entries** for fully-undocumented options
       (e.g. ``-hwaccel_output_format``, which has no texi entry on any
       supported tag). Synthesized from the ``OptionDef`` row: scope and
       valueType inferred from ``OPT_*`` flag tokens, description taken
       verbatim from the row's short C help string. The doc-derived list
       always wins on name collision — alias attachment happens first so
       a name that becomes an alias never also gets a top-level synthetic
       entry.

    Best-effort: a tag where every file fails to fetch just leaves the
    doc-derived options unchanged.
    """
    sources: dict[str, str] = {}
    for path in _OPTIONS_C_SOURCES:
        text = _load_text(repo, tag, path, fallback_root)
        if text:
            sources[path] = text
    if not sources:
        logger.debug("No fftools C sources available for option augmentation")
        return
    documented = {o["name"] for o in options}
    alias_map = build_alias_map(sources, documented)
    if alias_map:
        added = apply_alias_map(options, alias_map)
        if added:
            logger.debug(
                f"Augmented options.json with {added} aliases from "
                f"{', '.join(sources)}"
            )

    # Recompute "covered" from the post-alias state — any name that
    # build_alias_map just folded in as an alias must not also surface
    # as a top-level synthetic entry.
    covered: set[str] = set()
    for o in options:
        covered.add(o["name"])
        covered.update(o.get("aliases") or [])
    synthesized = build_undocumented_options(sources, covered)
    if synthesized:
        options.extend(synthesized)
        options.sort(key=lambda o: o["name"])
        logger.debug(
            f"Augmented options.json with {len(synthesized)} synthesized "
            f"entries for undocumented options from {', '.join(sources)}"
        )


def _extract_codecs(
    doc_root: Path,
    repo: Path,
    tag: str,
    fallback_root: Path | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
    x264_help: dict[str, UpstreamOptionHelp],
    x265_help: dict[str, UpstreamOptionHelp],
) -> list[dict]:
    root = _load_xml(doc_root, "codecs.texi", makeinfo_cmd, logger, xml_cache)
    codecs_from_doc = dedupe_codecs(parse_codecs_xml(root)) if root is not None else []

    codec_flags: dict[str, dict[str, bool]] = {}
    for source in ("libavcodec/allcodecs.c", "libavcodec/codec_list.c"):
        text = _load_text(repo, tag, source, fallback_root)
        if text:
            logger.debug(f"Parsing codec flags from {source}")
            codec_flags.update(parse_codecs_c(text))

    if not codecs_from_doc and not codec_flags:
        raise ExtractionError("Codec sources not found")

    # Merge: doc gives type + aliases for documented codecs, allcodecs.c
    # gives the comprehensive name list with encoder/decoder flags. Names
    # only present in allcodecs.c surface as type="video" (the historical
    # default — we don't try to infer type from the symbol name).
    merged = merge_codec_flags(codecs_from_doc, codec_flags)
    seen = {c.name for c in merged}
    for alias_set in (c.aliases for c in codecs_from_doc):
        seen.update(alias_set)

    extras = []
    for name, flags in sorted(codec_flags.items()):
        if name in seen:
            continue
        extras.append(
            {
                "name": name,
                "type": "video",
                "aliases": [],
                "encoder": flags.get("encoder", False),
                "decoder": flags.get("decoder", False),
                "anchor": "",
            }
        )

    documented = [
        {
            "name": c.name,
            "type": c.type,
            "aliases": c.aliases,
            "encoder": c.encoder,
            "decoder": c.decoder,
            "anchor": c.anchor,
        }
        for c in merged
    ]
    codecs = sorted(documented + extras, key=lambda c: c["name"])

    # Attach per-codec private options harvested from encoders.texi /
    # decoders.texi. Both sources are optional; older tags' docs may be
    # missing one or both, in which case the per-codec options[] just
    # stays empty on every entry.
    known_names: set[str] = set()
    for c in codecs:
        known_names.add(c["name"])
        for alias in c.get("aliases", []):
            known_names.add(alias)

    encoder_options: dict[str, list[AVOptionEntry]] = {}
    decoder_options: dict[str, list[AVOptionEntry]] = {}
    enc_root = _load_xml(doc_root, "encoders.texi", makeinfo_cmd, logger, xml_cache)
    if enc_root is not None:
        logger.debug("Parsing per-codec encoder options from encoders.texi")
        encoder_options = parse_per_codec_options_xml(enc_root, "encoder", known_names)
    dec_root = _load_xml(doc_root, "decoders.texi", makeinfo_cmd, logger, xml_cache)
    if dec_root is not None:
        logger.debug("Parsing per-codec decoder options from decoders.texi")
        decoder_options = parse_per_codec_options_xml(dec_root, "decoder", known_names)

    per_codec = merge_per_codec_options(encoder_options, decoder_options)
    for c in codecs:
        # Aliases share an options table — when ``parse_codecs_xml`` collapsed
        # ``libx264, libx264rgb`` into a single entry, the per-codec parser
        # may have keyed its options under either name. Try the canonical
        # first, then any aliases, and emit the first hit.
        opts: list[AVOptionEntry] = per_codec.get(c["name"], [])
        if not opts:
            for alias in c.get("aliases", []):
                opts = per_codec.get(alias, [])
                if opts:
                    break
        # Overlay AV_OPT_TYPE_CONST value descriptions from the C source.
        # The lookup tries the canonical codec name plus all aliases; the
        # first AVClass-bound match wins.
        enriched = enrich_options_with_c_values(
            opts, c_map, [c["name"], *c.get("aliases", [])]
        )
        c["options"] = [_av_option_to_dict(o) for o in enriched]
        # Fill values+descriptions for libx264/libx265's string-typed
        # passthrough options (-preset / -tune / -profile) from the
        # upstream library sources. Both calls are no-ops for codecs
        # outside their respective family.
        layer_upstream_string_values(c, x264_help, X264_FAMILY, "x264")
        layer_upstream_string_values(c, x265_help, X265_FAMILY, "x265")

    return codecs


def _av_option_to_dict(o) -> dict:
    # ``value_descriptions`` is normalized to len(values) before serialization
    # so the SPA can pair them index-by-index without bounds checks.
    descs = list(o.value_descriptions)
    if len(descs) < len(o.values):
        descs.extend([""] * (len(o.values) - len(descs)))
    elif len(descs) > len(o.values):
        descs = descs[: len(o.values)]
    return {
        "name": o.name,
        "aliases": o.aliases,
        "valueType": o.value_type,
        "values": o.values,
        "valueDescriptions": descs,
        "description": o.description,
        "anchor": o.anchor,
        "signature": o.signature,
        "roles": o.roles,
    }


def _extract_codec_options(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Parse the generic AVCodec options chapter from ``codecs.texi``.

    Missing/unparseable source returns an empty list — older tags' docs may
    not carry the chapter, and we don't want to fail extraction over it.
    The SPA tolerates an absent ``codec_options`` array.
    """
    root = _load_xml(doc_root, "codecs.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.debug("codecs.texi not found; codec_options will be empty")
        return []
    logger.debug("Parsing codec_options from codecs.texi")
    options = dedupe_av_options(parse_codec_options_xml(root))
    options = enrich_options_with_c_values(options, c_map, [AVCODEC_GLOBAL_KEY])
    return [_av_option_to_dict(o) for o in options]


def _attach_per_format_options(
    entries: list[dict],
    doc_root: Path,
    source_file: str,
    side: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> None:
    """Enrich a list of muxer/demuxer dicts in place with an ``options`` field
    sourced from ``muxers.texi`` / ``demuxers.texi``.

    Missing source: every entry's ``options`` becomes an empty list. The SPA
    tolerates absence anyway, so older bundles aren't affected.
    """
    known: set[str] = set()
    for e in entries:
        known.add(e["name"])
        for alias in e.get("aliases", []):
            known.add(alias)

    by_name: dict[str, list[AVOptionEntry]] = {}
    root = _load_xml(doc_root, source_file, makeinfo_cmd, logger, xml_cache)
    if root is not None:
        logger.debug(f"Parsing per-{side} options from {source_file}")
        by_name = parse_per_format_options_xml(root, side, known)

    for e in entries:
        opts: list[AVOptionEntry] = by_name.get(e["name"], [])
        if not opts:
            for alias in e.get("aliases", []):
                opts = by_name.get(alias, [])
                if opts:
                    break
        enriched = enrich_options_with_c_values(
            opts, c_map, [e["name"], *e.get("aliases", [])]
        )
        e["options"] = [_av_option_to_dict(o) for o in enriched]


def _extract_format_options(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Parse the generic AVFormat options chapter from ``formats.texi``."""
    root = _load_xml(doc_root, "formats.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.debug("formats.texi not found; format_options will be empty")
        return []
    logger.debug("Parsing format_options from formats.texi")
    options = dedupe_av_options(parse_format_options_xml(root))
    options = enrich_options_with_c_values(options, c_map, [AVFORMAT_GLOBAL_KEY])
    return [_av_option_to_dict(o) for o in options]


def _extract_filters(
    doc_root: Path,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    root = _load_xml(doc_root, "filters.texi", makeinfo_cmd, logger, xml_cache)
    if root is None:
        raise ExtractionError("Filter sources not found")
    logger.debug("Parsing filters from filters.texi")
    filters = dedupe_filters(parse_filters_xml(root))
    return [
        {
            "name": f.name,
            "type": f.type,
            "aliases": f.aliases,
            "params": f.params,
            "description": f.description,
            "args": f.args,
        }
        for f in filters
    ]


def _extract_named(
    doc_root: Path,
    source_file: str,
    parser,
    category: str,
    makeinfo_cmd: list[str],
    logger: Logger,
    xml_cache: dict[str, ET.Element | None],
) -> list[dict]:
    """Run ``parser`` against ``doc_root/doc/<source_file>`` and serialize.

    Returns an empty list (and warns) if the source texi is missing, so a
    single unavailable catalog doesn't fail the whole extraction. Hard
    parse/makeinfo failures still surface as warnings via ``_load_xml``.
    """
    root = _load_xml(doc_root, source_file, makeinfo_cmd, logger, xml_cache)
    if root is None:
        logger.warn(f"{category} source not found ({source_file})")
        return []
    logger.debug(f"Parsing {category} from {source_file}")
    entries = dedupe_named(parser(root))
    return [
        {
            "name": e.name,
            "aliases": e.aliases,
            "anchor": e.anchor,
            "description": e.description,
        }
        for e in entries
    ]


def _merge_named_dicts(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Merge two serialized ``NamedEntry`` lists, primary entries winning on
    name collision. Used to fold input/output devices into the demuxers/muxers
    bundles without losing the demuxer-side description when a name appears in
    both (e.g. ``fbdev`` is documented as both an indev and an outdev).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for entry in (*primary, *extra):
        name = entry.get("name", "")
        if name in seen:
            continue
        seen.add(name)
        # Entries coming from device sources skipped _attach_per_format_options
        # (which only knows muxers/demuxers texis) — give them an empty options
        # list so the SPA's shape expectation holds across all entries.
        entry.setdefault("options", [])
        out.append(entry)
    out.sort(key=lambda e: e.get("name", ""))
    return out


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _build_index(
    version: str,
    released: str | None,
    categories: set[str],
    *,
    tag: str = "",
    x264_doc: str = "",
    x265_doc: str = "",
) -> dict:
    index: dict = {
        "version": version,
        "released": released or "",
    }
    # The exact upstream git tag this bundle was extracted from (e.g.
    # ``n8.1.1``). ``version`` is rolled up to ``major.minor`` under
    # ``--latest-per-minor``, so the patch is only recoverable from here —
    # the third-party notices page needs it to cite the precise source.
    if tag:
        index["tag"] = tag
    # Only advertise files this run actually produced. Older bundles on disk
    # carried only options/codecs/filters; the SPA must tolerate the absence
    # of the value-lookup keys, which is exactly what omitting them signals.
    if "options" in categories:
        index["options"] = "options.json"
    if "codecs" in categories:
        index["codecs"] = "codecs.json"
    if "filters" in categories:
        index["filters"] = "filters.json"
    if "demuxers" in categories:
        index["demuxers"] = "demuxers.json"
    if "muxers" in categories:
        index["muxers"] = "muxers.json"
    if "protocols" in categories:
        index["protocols"] = "protocols.json"
    if "bitstream_filters" in categories:
        index["bitstream_filters"] = "bitstream_filters.json"
    # Path is relative to the SPA's public root (``web/public/``), not
    # to this index.json's directory — the file lives in a sibling tree
    # (``doc/x264/<commit>/``) keyed on x264 commit so multiple FFmpeg
    # versions pinning the same x264 commit share one rendered file.
    # Absent when --x264-repo wasn't supplied for this run.
    if x264_doc:
        index["x264_doc"] = x264_doc
    if x265_doc:
        index["x265_doc"] = x265_doc
    return index


def _extract_for_tag(
    config: ExtractConfig, tag: str, makeinfo_cmd: list[str], logger: Logger
) -> None:
    version = parse_tag_version(tag)
    if version is None:
        raise ExtractionError(f"Invalid tag format: {tag}")

    major, minor, patch = version
    target_version = f"{major}.{minor}"
    released = tag_date_iso(config.repo, tag)

    try:
        _extract_and_write(config, tag, target_version, released, makeinfo_cmd, logger, None)
        return
    except ExtractionError as exc:
        if not config.worktree_fallback:
            raise
        logger.warn(f"Primary extraction failed for {tag}: {exc}")

    try:
        with temporary_worktree(config.repo, tag) as root:
            _extract_and_write(
                config, tag, target_version, released, makeinfo_cmd, logger, root
            )
    except Exception as exc:
        raise ExtractionError(f"Worktree fallback failed: {exc}")


def _extract_and_write(
    config: ExtractConfig,
    tag: str,
    target_version: str,
    released: str | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    fallback_root: Path | None,
) -> None:
    output_dir = config.out / "metadata" / "ffmpeg" / target_version
    logger.info(f"Extracting {tag} -> {output_dir}")

    with staged_doc(config.repo, tag, target_version, fallback_root) as doc_root:
        # AVOption value descriptions come from the libav* C source, which
        # is staged alongside ``doc/`` by ``_stage_doc_dir``. Missing trees
        # (very old tag, archive failure) collapse to an empty map — the
        # texi-derived options still surface, just without enum descriptions.
        c_map = build_class_options_map(
            (doc_root / "libavcodec", doc_root / "libavformat")
        )
        if c_map:
            logger.debug(f"Parsed AVOption tables for {len(c_map)} classes")
        else:
            logger.debug("No AVOption tables parsed from libav* sources")

        # Memoize ``makeinfo --xml`` output per filename for this tag —
        # several .texi files are parsed twice (codecs.texi for codecs +
        # codec_options; muxers.texi / demuxers.texi each for the catalog
        # and the per-entity options pass) and re-running ``makeinfo`` is
        # the bulk of per-tag wall time. ``None`` is cached too so a
        # repeated lookup doesn't re-emit the makeinfo-failed warning.
        xml_cache: dict[str, ET.Element | None] = {}

        # Resolve, parse, cache, log, render, and emit each upstream
        # library's reference. Returns ({}, "") when the repo flag
        # wasn't passed or no contemporaneous snapshot exists.
        x264_help, x264_doc_path = enrich_x264(config, released, logger)
        x265_help, x265_doc_path = enrich_x265(config, released, logger)

        if "options" in config.categories:
            options = _extract_options(
                doc_root, config.repo, tag, fallback_root,
                makeinfo_cmd, logger, xml_cache,
            )
            _write_json(output_dir / "options.json", {"options": options})

        if "codecs" in config.categories:
            codecs = _extract_codecs(
                doc_root, config.repo, tag, fallback_root, makeinfo_cmd, logger,
                c_map, xml_cache, x264_help, x265_help,
            )
            codec_options = _extract_codec_options(
                doc_root, makeinfo_cmd, logger, c_map, xml_cache,
            )
            _write_json(
                output_dir / "codecs.json",
                {"codec_options": codec_options, "codecs": codecs},
            )

        if "filters" in config.categories:
            filters = _extract_filters(doc_root, makeinfo_cmd, logger, xml_cache)
            _write_json(output_dir / "filters.json", {"filters": filters})

        if "demuxers" in config.categories:
            demuxers = _extract_named(
                doc_root, "demuxers.texi", parse_demuxers_xml, "demuxers",
                makeinfo_cmd, logger, xml_cache,
            )
            _attach_per_format_options(
                demuxers, doc_root, "demuxers.texi", "demuxer",
                makeinfo_cmd, logger, c_map, xml_cache,
            )
            # Input devices are selected with the same ``-f`` flag as demuxers
            # (libavdevice exposes them as demuxers at runtime), so they're
            # merged into the demuxers bundle. Without this the SPA emits a
            # spurious ``unknown-demuxer`` warning for ``-f lavfi`` etc.
            indevs = _extract_named(
                doc_root, "indevs.texi", parse_input_devices_xml,
                "input devices", makeinfo_cmd, logger, xml_cache,
            )
            demuxers = _merge_named_dicts(demuxers, indevs)
            _write_json(output_dir / "demuxers.json", {"demuxers": demuxers})

        if "muxers" in config.categories:
            muxers = _extract_named(
                doc_root, "muxers.texi", parse_muxers_xml, "muxers",
                makeinfo_cmd, logger, xml_cache,
            )
            _attach_per_format_options(
                muxers, doc_root, "muxers.texi", "muxer",
                makeinfo_cmd, logger, c_map, xml_cache,
            )
            outdevs = _extract_named(
                doc_root, "outdevs.texi", parse_output_devices_xml,
                "output devices", makeinfo_cmd, logger, xml_cache,
            )
            muxers = _merge_named_dicts(muxers, outdevs)
            format_options = _extract_format_options(
                doc_root, makeinfo_cmd, logger, c_map, xml_cache,
            )
            _write_json(
                output_dir / "muxers.json",
                {"format_options": format_options, "muxers": muxers},
            )

        if "protocols" in config.categories:
            protocols = _extract_named(
                doc_root, "protocols.texi", parse_protocols_xml, "protocols",
                makeinfo_cmd, logger, xml_cache,
            )
            _write_json(output_dir / "protocols.json", {"protocols": protocols})

        if "bitstream_filters" in config.categories:
            bsfs = _extract_named(
                doc_root, "bitstream_filters.texi", parse_bitstream_filters_xml,
                "bitstream_filters", makeinfo_cmd, logger, xml_cache,
            )
            _write_json(
                output_dir / "bitstream_filters.json",
                {"bitstream_filters": bsfs},
            )

        if config.html_doc:
            generate_html_doc(
                doc_root, config.out, config.repo, target_version, tag,
                makeinfo_cmd, logger,
            )

    index = _build_index(
        target_version, released, config.categories,
        tag=tag, x264_doc=x264_doc_path, x265_doc=x265_doc_path,
    )
    _write_json(output_dir / "index.json", index)


def run_extraction(config: ExtractConfig) -> int:
    logger = Logger(config.verbose)

    if not config.repo.exists():
        logger.warn("Repository path does not exist")
        return 1

    if not (config.repo / ".git").exists():
        logger.warn("Repository path does not look like a Git repo")
        return 1

    try:
        makeinfo_cmd = resolve_makeinfo()
    except MakeinfoError as exc:
        logger.warn(str(exc))
        return 1
    logger.debug(f"Using makeinfo: {' '.join(makeinfo_cmd)}")

    try:
        tags = select_tags(config, logger)
    except ExtractionError as exc:
        logger.warn(str(exc))
        return 2

    failures: list[str] = []
    worker_count = max(1, min(config.jobs, len(tags)))

    if worker_count <= 1:
        for tag in tags:
            tag_logger = Logger(config.verbose, tag=tag)
            try:
                _extract_for_tag(config, tag, makeinfo_cmd, tag_logger)
            except ExtractionError as exc:
                tag_logger.warn(f"Extraction failed: {exc}")
                failures.append(tag)
                if not config.continue_on_error:
                    return 3
    else:
        logger.debug(f"Extracting {len(tags)} tags with {worker_count} workers")
        # ProcessPool, not ThreadPool: per-tag work is dominated by external
        # ``makeinfo`` invocations (already separate processes) plus Python
        # parsing of the resulting XML and the libav* C source — the latter
        # is CPU-bound and would contend on the GIL with threads. Each
        # worker gets its own tempdir per tag, so file-system isolation is
        # already in place.
        #
        # Workers ship their log lines to the parent over ``log_queue``; a
        # parent-side drain thread is the sole console writer (streaming, no
        # buffering). A shared lock alone wouldn't fix the multi-process
        # console tearing on Windows (the ``♪◙`` regression) — only one
        # process writing does.
        #
        # We don't pre-flight ``--continue-on-error: False``: with multiple
        # tags already in flight, "bail at first error" can't unwind the
        # in-flight work. Instead, we let all submitted futures finish and
        # then return non-zero — same exit code as before, slightly more
        # work done than necessary in the failure case.
        mp_ctx = multiprocessing.get_context()
        log_queue = mp_ctx.Queue()
        upstream_doc_lock = mp_ctx.Lock()
        drain_thread = threading.Thread(
            target=drain_log_queue, args=(log_queue,), daemon=True
        )
        drain_thread.start()
        try:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                mp_context=mp_ctx,
                initializer=_pool_initializer,
                initargs=(log_queue, upstream_doc_lock),
            ) as pool:
                future_to_tag = {
                    pool.submit(_extract_for_tag_pooled, config, tag, makeinfo_cmd): tag
                    for tag in tags
                }
                for future in concurrent.futures.as_completed(future_to_tag):
                    tag = future_to_tag[future]
                    try:
                        ok = future.result()
                    except Exception as exc:
                        # The worker function itself failed to *return* —
                        # segfault, pickle error, OOM. No log surfaces; report
                        # the crash and move on. ``warn`` carries the
                        # ``[{tag}]`` prefix like the rest.
                        logger.warn(
                            f"[{tag}] Worker died: {type(exc).__name__}: {exc}"
                        )
                        failures.append(tag)
                        continue
                    if not ok:
                        failures.append(tag)
        finally:
            # Pool is shut down (workers exited gracefully and flushed their
            # queued lines); stop the drain thread once it has written them.
            log_queue.put(None)
            drain_thread.join()

    # Third-party license texts + aggregate notices page. Emitted once in the
    # parent regardless of per-tag failures (any artifact that did land must
    # still carry its attribution); the notices page reflects whatever
    # snapshots actually made it to the output tree.
    ensure_license_texts(config, logger)
    generate_notices_page(config, logger)

    if failures:
        logger.warn(f"Extraction failed for tags: {', '.join(failures)}")
        return 3

    return 0
