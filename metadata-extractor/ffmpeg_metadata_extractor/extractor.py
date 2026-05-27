from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from .avopt_c import (
    AVCODEC_GLOBAL_KEY,
    AVFORMAT_GLOBAL_KEY,
    ParsedOption,
    build_class_options_map,
    enrich_options_with_c_values,
)
from .config_texi import generate_config_texi
from .git_utils import list_tags, show_file, tag_date_iso, temporary_worktree
from .models import AVOptionEntry, ExtractConfig
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
    parse_muxers_xml,
    parse_options_xml,
    parse_per_codec_options_xml,
    parse_per_format_options_xml,
    parse_protocols_xml,
)
from .texi_xml import MakeinfoError, resolve_makeinfo, run_makeinfo, run_makeinfo_html

_TAG_PATTERN = re.compile(r"^n(\d+)\.(\d+)\.(\d+)$")

_ASSETS_DIR = Path(__file__).parent / "assets"
_SHARED_CSS_FILES = ("bootstrap.min.css", "style.min.css")
_VENDORED_T2H = "t2h.pm"


# Cross-process print lock for parallel workers. ``None`` in the parent and
# in sequential runs — only set in pool workers via :func:`_pool_initializer`
# below. When set, every Logger emit serializes its single ``write() + flush()``
# under this lock so concurrent workers don't tear each other's lines apart.
# (Plain ``print()`` does two writes — payload and newline — and on Windows
# with multiple processes inheriting the same stdout handle, those writes can
# interleave at the byte level. The visible symptom was raw ``\\r\\n`` bytes
# mid-line, rendered as ``♪◙`` by CP437-savvy terminals.)
_print_lock = None


def _pool_initializer(lock) -> None:
    """Runs once per worker process at pool start-up. Stashes the shared
    lock in the module-level slot so every Logger instance in this worker
    serializes its writes through it."""
    global _print_lock
    _print_lock = lock


def _write_line(stream: str, line: str) -> None:
    """Single ``write()+flush()`` (under the shared lock if pooled). One
    syscall per line is the smallest interleave-resistant unit available
    without buffering; the lock is the extra guard for the multi-worker case.
    """
    target = sys.stderr if stream == "stderr" else sys.stdout
    if _print_lock is not None:
        with _print_lock:
            target.write(line + "\n")
            target.flush()
    else:
        target.write(line + "\n")
        target.flush()


class Logger:
    """Per-tag log emitter.

    ``tag`` (when set) is prepended to every line as ``[{tag}] `` so output
    from concurrently-extracting workers stays attributable. Writes happen
    immediately — no buffering — so users see progress as it streams. In
    pooled mode the writes are serialized via ``_print_lock``.
    """

    def __init__(self, verbose: bool, *, tag: str | None = None) -> None:
        self._verbose = verbose
        self._tag = tag

    def _emit(self, stream: str, message: str) -> None:
        line = f"[{self._tag}] {message}" if self._tag else message
        _write_line(stream, line)

    def info(self, message: str) -> None:
        self._emit("stdout", message)

    def debug(self, message: str) -> None:
        if self._verbose:
            self._emit("stdout", message)

    def warn(self, message: str) -> None:
        self._emit("stderr", f"WARNING: {message}")


def _extract_for_tag_pooled(
    config: ExtractConfig, tag: str, makeinfo_cmd: list[str]
) -> bool:
    """Worker entry point for ``--jobs > 1`` extraction.

    The worker writes its log lines directly (under the shared lock set up
    in :func:`_pool_initializer`) so users see streaming progress. Returns
    ``True`` on success, ``False`` after warning about the failure — the
    parent only needs the boolean to track failures.
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


class ExtractionError(Exception):
    pass


def parse_tag_version(tag: str) -> tuple[int, int, int] | None:
    match = _TAG_PATTERN.match(tag)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def semver_key(tag: str) -> tuple[int, int, int]:
    version = parse_tag_version(tag)
    if version is None:
        return (0, 0, 0)
    return version


def select_tags(config: ExtractConfig, logger: Logger) -> list[str]:
    tags = list_tags(config.repo)
    tags = [t for t in tags if parse_tag_version(t)]

    if config.tags:
        selected = [t for t in config.tags if t in tags]
        missing = [t for t in config.tags if t not in tags]
        if missing:
            raise ExtractionError(f"Tags not found: {', '.join(missing)}")
    elif config.tag_range:
        start, end = config.tag_range
        start_v = parse_tag_version(start)
        end_v = parse_tag_version(end)
        if start_v is None or end_v is None:
            raise ExtractionError("Range tags must match n<major>.<minor>.<patch>")
        selected = [t for t in tags if start_v <= parse_tag_version(t) <= end_v]  # type: ignore
        if not selected:
            raise ExtractionError("No tags found within range")
    else:
        selected = tags

    selected.sort(key=semver_key)

    if not config.latest_per_minor:
        return selected

    latest: dict[tuple[int, int], str] = {}
    for tag in selected:
        major, minor, patch = semver_key(tag)
        key = (major, minor)
        current = latest.get(key)
        if current is None or semver_key(tag) > semver_key(current):
            latest[key] = tag

    result = sorted(latest.values(), key=semver_key)
    logger.debug(f"Selected tags: {', '.join(result)}")
    return result


def _stage_subtrees(repo: Path, tag: str, dest: Path, subtrees: tuple[str, ...]) -> bool:
    """Materialize the given subtrees of ``tag`` into ``dest``. Returns
    True iff every subtree extracts and the first one — historically
    ``doc/`` — is present afterward.

    Uses ``git archive`` to avoid touching the working tree. The
    available-subtree-set differs across older tags (e.g. libavformat
    moved files around); a missing optional subtree is logged by the
    caller, not failed here, so callers should only check existence of
    the doc subtree (which is the hard requirement).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", tag, *subtrees],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    try:
        # Pipe the tar stream into ``tar -x`` inside ``dest``. We rely on the
        # tar that ships with both Git for Windows and Unix systems.
        tar = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            input=proc.stdout,
            capture_output=True,
            check=True,
        )
        _ = tar
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return (dest / subtrees[0]).is_dir()


def _stage_doc_dir(repo: Path, tag: str, dest: Path) -> bool:
    """Compat wrapper — stage ``doc/`` plus the libav source trees we
    use for AVOption parsing. ``libavcodec``/``libavformat`` are
    best-effort: their absence on a given tag will silently disable
    C-source value enrichment, not the whole extraction."""
    return _stage_subtrees(repo, tag, dest, ("doc", "libavcodec", "libavformat"))


def _write_dummy_config_texi(repo: Path, tag: str, doc_dir: Path, version: str) -> None:
    """Write a generated ``config.texi`` into ``doc_dir`` if absent.

    The real file is produced by ``./configure`` at build time; we synthesize
    a feature-everything-enabled stand-in by parsing ``configure``. This
    lets ``makeinfo`` resolve ``@include config.texi`` and evaluate every
    ``@ifset config-…`` conditional in the docs.
    """
    target = doc_dir / "config.texi"
    if target.exists():
        return
    configure_text = show_file(repo, tag, "configure")
    if not configure_text:
        return
    target.write_text(
        generate_config_texi(configure_text, version),
        encoding="utf-8",
    )


@contextmanager
def _staged_doc(repo: Path, tag: str, version: str, fallback_root: Path | None):
    """Yield a directory containing ``doc/`` for ``tag``.

    Tries ``git archive`` against ``repo`` first. Falls back to the
    pre-extracted worktree ``fallback_root`` if provided. A synthetic
    ``config.texi`` is written into the staged ``doc/`` so makeinfo can
    resolve ``@include config.texi`` without running ``./configure``.
    """
    if fallback_root is not None and (fallback_root / "doc").is_dir():
        _write_dummy_config_texi(repo, tag, fallback_root / "doc", version)
        yield fallback_root
        return

    tmp = Path(tempfile.mkdtemp(prefix="ffmpeg-doc-"))
    try:
        if not _stage_doc_dir(repo, tag, tmp):
            raise ExtractionError(f"Could not stage doc/ for tag {tag}")
        _write_dummy_config_texi(repo, tag, tmp / "doc", version)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        return [
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
    raise ExtractionError("Options sources not found")


def _extract_codecs(
    doc_root: Path,
    repo: Path,
    tag: str,
    fallback_root: Path | None,
    makeinfo_cmd: list[str],
    logger: Logger,
    c_map: dict[str, list[ParsedOption]],
    xml_cache: dict[str, ET.Element | None],
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


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _build_index(version: str, released: str | None, categories: set[str]) -> dict:
    index: dict = {
        "version": version,
        "released": released or "",
    }
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

    with _staged_doc(config.repo, tag, target_version, fallback_root) as doc_root:
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

        if "options" in config.categories:
            options = _extract_options(doc_root, makeinfo_cmd, logger, xml_cache)
            _write_json(output_dir / "options.json", {"options": options})

        if "codecs" in config.categories:
            codecs = _extract_codecs(
                doc_root, config.repo, tag, fallback_root, makeinfo_cmd, logger,
                c_map, xml_cache,
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
            _generate_html_doc(
                doc_root, config.out, target_version, makeinfo_cmd, logger
            )

    index = _build_index(target_version, released, config.categories)
    _write_json(output_dir / "index.json", index)


def _generate_html_doc(
    doc_root: Path,
    out: Path,
    target_version: str,
    makeinfo_cmd: list[str],
    logger: Logger,
) -> None:
    src = doc_root / "doc" / "ffmpeg.texi"
    if not src.exists():
        logger.warn(f"Skipping HTML doc for {target_version}: ffmpeg.texi missing")
        return

    # Overlay the vendored t2h.pm into the staged doc/. The tag's own t2h.pm
    # may target an older Texinfo API (n7.x and earlier call $self->gdt which
    # Texinfo 7.1+ removed); the vendored copy is version-gated and references
    # the shared CSS at ../bootstrap.min.css / ../style.min.css so the HTML
    # can live under {out}/doc/ffmpeg/{version}/ next to the shared assets.
    staged_t2h = src.parent / _VENDORED_T2H
    shutil.copyfile(_ASSETS_DIR / _VENDORED_T2H, staged_t2h)

    doc_root_out = out / "doc" / "ffmpeg"
    _ensure_shared_assets(doc_root_out)

    output_path = doc_root_out / target_version / "ffmpeg-all.html"
    logger.info(f"Rendering HTML doc -> {output_path}")
    try:
        run_makeinfo_html(
            src,
            output_path,
            cwd=src.parent,
            cmd=makeinfo_cmd,
            init_file=staged_t2h,
        )
    except MakeinfoError as exc:
        logger.warn(f"HTML doc generation failed for {target_version}: {exc}")


def _ensure_shared_assets(doc_root_out: Path) -> None:
    """Copy the shared CSS files into ``doc_root_out`` if missing or stale."""
    doc_root_out.mkdir(parents=True, exist_ok=True)
    for name in _SHARED_CSS_FILES:
        src = _ASSETS_DIR / name
        dst = doc_root_out / name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copyfile(src, dst)


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
        # Each worker prints its log lines directly (streaming, no buffering)
        # under a shared lock installed by ``_pool_initializer`` — that's how
        # we keep concurrent stdout writes from interleaving at the byte level
        # while still letting users watch progress.
        #
        # We don't pre-flight ``--continue-on-error: False``: with multiple
        # tags already in flight, "bail at first error" can't unwind the
        # in-flight work. Instead, we let all submitted futures finish and
        # then return non-zero — same exit code as before, slightly more
        # work done than necessary in the failure case.
        mp_ctx = multiprocessing.get_context()
        print_lock = mp_ctx.Lock()
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=mp_ctx,
            initializer=_pool_initializer,
            initargs=(print_lock,),
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
                    # The worker function itself failed to *return* — segfault,
                    # pickle error, OOM. No log surfaces; report the crash and
                    # move on. Use ``info``-style write so the line carries the
                    # ``[{tag}]`` prefix like the rest.
                    logger.warn(
                        f"[{tag}] Worker died: {type(exc).__name__}: {exc}"
                    )
                    failures.append(tag)
                    continue
                if not ok:
                    failures.append(tag)

    if failures:
        logger.warn(f"Extraction failed for tags: {', '.join(failures)}")
        return 3

    return 0
