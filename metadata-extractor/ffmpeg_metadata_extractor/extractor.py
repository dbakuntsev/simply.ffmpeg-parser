from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from .config_texi import generate_config_texi
from .git_utils import list_tags, show_file, tag_date_iso, temporary_worktree
from .models import ExtractConfig
from .parsing import (
    dedupe_codecs,
    dedupe_filters,
    dedupe_named,
    dedupe_options,
    merge_codec_flags,
    parse_bitstream_filters_xml,
    parse_codecs_c,
    parse_codecs_xml,
    parse_demuxers_xml,
    parse_filters_xml,
    parse_muxers_xml,
    parse_options_xml,
    parse_protocols_xml,
)
from .texi_xml import MakeinfoError, resolve_makeinfo, run_makeinfo, run_makeinfo_html

_TAG_PATTERN = re.compile(r"^n(\d+)\.(\d+)\.(\d+)$")

_ASSETS_DIR = Path(__file__).parent / "assets"
_SHARED_CSS_FILES = ("bootstrap.min.css", "style.min.css")
_VENDORED_T2H = "t2h.pm"


class Logger:
    def __init__(self, verbose: bool) -> None:
        self._verbose = verbose

    def info(self, message: str) -> None:
        print(message)

    def debug(self, message: str) -> None:
        if self._verbose:
            print(message)

    def warn(self, message: str) -> None:
        print(f"WARNING: {message}", file=sys.stderr)


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


def _stage_doc_dir(repo: Path, tag: str, dest: Path) -> bool:
    """Materialize ``doc/`` for ``tag`` into ``dest``. Returns True on success.

    Uses ``git archive`` to extract just the ``doc/`` subtree without
    touching the working tree.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", tag, "doc"],
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
    return (dest / "doc").is_dir()


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


def _load_xml(doc_root: Path, filename: str, makeinfo_cmd: list[str], logger: Logger) -> ET.Element | None:
    src = doc_root / "doc" / filename
    if not src.exists():
        return None
    try:
        return run_makeinfo(src, cwd=src.parent, cmd=makeinfo_cmd)
    except MakeinfoError as exc:
        logger.warn(f"makeinfo failed on {filename}: {exc}")
        return None


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


def _extract_options(doc_root: Path, makeinfo_cmd: list[str], logger: Logger) -> list[dict]:
    for source in ("ffmpeg.texi", "ffmpeg-all.texi", "ffmpeg-opt.texi"):
        root = _load_xml(doc_root, source, makeinfo_cmd, logger)
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
) -> list[dict]:
    root = _load_xml(doc_root, "codecs.texi", makeinfo_cmd, logger)
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
    return sorted(documented + extras, key=lambda c: c["name"])


def _extract_filters(doc_root: Path, makeinfo_cmd: list[str], logger: Logger) -> list[dict]:
    root = _load_xml(doc_root, "filters.texi", makeinfo_cmd, logger)
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
) -> list[dict]:
    """Run ``parser`` against ``doc_root/doc/<source_file>`` and serialize.

    Returns an empty list (and warns) if the source texi is missing, so a
    single unavailable catalog doesn't fail the whole extraction. Hard
    parse/makeinfo failures still surface as warnings via ``_load_xml``.
    """
    root = _load_xml(doc_root, source_file, makeinfo_cmd, logger)
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
        if "options" in config.categories:
            options = _extract_options(doc_root, makeinfo_cmd, logger)
            _write_json(output_dir / "options.json", {"options": options})

        if "codecs" in config.categories:
            codecs = _extract_codecs(
                doc_root, config.repo, tag, fallback_root, makeinfo_cmd, logger
            )
            _write_json(output_dir / "codecs.json", {"codecs": codecs})

        if "filters" in config.categories:
            filters = _extract_filters(doc_root, makeinfo_cmd, logger)
            _write_json(output_dir / "filters.json", {"filters": filters})

        if "demuxers" in config.categories:
            demuxers = _extract_named(
                doc_root, "demuxers.texi", parse_demuxers_xml, "demuxers",
                makeinfo_cmd, logger,
            )
            _write_json(output_dir / "demuxers.json", {"demuxers": demuxers})

        if "muxers" in config.categories:
            muxers = _extract_named(
                doc_root, "muxers.texi", parse_muxers_xml, "muxers",
                makeinfo_cmd, logger,
            )
            _write_json(output_dir / "muxers.json", {"muxers": muxers})

        if "protocols" in config.categories:
            protocols = _extract_named(
                doc_root, "protocols.texi", parse_protocols_xml, "protocols",
                makeinfo_cmd, logger,
            )
            _write_json(output_dir / "protocols.json", {"protocols": protocols})

        if "bitstream_filters" in config.categories:
            bsfs = _extract_named(
                doc_root, "bitstream_filters.texi", parse_bitstream_filters_xml,
                "bitstream_filters", makeinfo_cmd, logger,
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

    for tag in tags:
        try:
            _extract_for_tag(config, tag, makeinfo_cmd, logger)
        except ExtractionError as exc:
            logger.warn(f"Extraction failed for {tag}: {exc}")
            failures.append(tag)
            if not config.continue_on_error:
                return 3

    if failures:
        logger.warn(f"Extraction failed for tags: {', '.join(failures)}")
        return 3

    return 0
