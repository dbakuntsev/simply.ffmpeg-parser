"""Render the FFmpeg ``ffmpeg-all.html`` reference page via
``makeinfo --html``, with the pinned-tag ``t2h.pm`` init file repointed
so every version's page shares one CSS pair under ``<out>/doc/ffmpeg/``,
and append the LGPL attribution footer."""

from __future__ import annotations

from pathlib import Path

from ._logging import Logger
from .attribution import DOC_TO_ROOT, NOTICES_FILENAME, THIRD_PARTY
from .pinned_assets import PINNED_ASSET_TAG, ensure_shared_assets, pinned_asset_bytes
from .texi_xml import MakeinfoError, run_makeinfo_html
from .upstream_help import build_generated_doc_footer

_T2H_DOC_PATH = "doc/t2h.pm"

# n8.1.1's doc/t2h.pm emits two stylesheet <link>s pointing at CSS sitting
# beside the generated HTML. We repoint each one directory up so every
# version's page under doc/ffmpeg/<version>/ shares a single CSS pair at
# doc/ffmpeg/. This is the only modification made to the upstream file, applied
# here in code (MIT) rather than to a committed copy. Each replacement must hit
# exactly once; a miss means the pinned init file changed shape and the repoint
# silently no-opped, so we skip HTML with a loud warning instead.
_T2H_HREF_REPOINTS = (
    ('href="bootstrap.min.css"', 'href="../bootstrap.min.css"'),
    ('href="style.min.css"', 'href="../style.min.css"'),
)


def generate_html_doc(
    doc_root: Path,
    out: Path,
    repo: Path,
    target_version: str,
    tag: str,
    makeinfo_cmd: list[str],
    logger: Logger,
) -> None:
    src = doc_root / "doc" / "ffmpeg.texi"
    if not src.exists():
        logger.warn(f"Skipping HTML doc for {target_version}: ffmpeg.texi missing")
        return

    # Stage the pinned-tag t2h.pm into the staged doc/, repointing its two CSS
    # hrefs at the shared copies one directory up (see _T2H_* constants). The
    # tag's own t2h.pm is not used: older tags target a removed Texinfo API.
    t2h_bytes = pinned_asset_bytes(repo, _T2H_DOC_PATH)
    if t2h_bytes is None:
        logger.warn(
            f"Skipping HTML doc for {target_version}: {PINNED_ASSET_TAG}:"
            f"{_T2H_DOC_PATH} not found in {repo} — is the {PINNED_ASSET_TAG} "
            "tag present in --repo?"
        )
        return

    t2h_text = t2h_bytes.decode("utf-8")
    for old, new in _T2H_HREF_REPOINTS:
        if old not in t2h_text:
            logger.warn(
                f"Skipping HTML doc for {target_version}: expected CSS href "
                f"{old!r} not found in {PINNED_ASSET_TAG}:{_T2H_DOC_PATH} — the "
                "pinned init file changed shape; update _T2H_HREF_REPOINTS."
            )
            return
        t2h_text = t2h_text.replace(old, new)

    staged_t2h = src.parent / "t2h.pm"
    staged_t2h.write_text(t2h_text, encoding="utf-8")

    doc_root_out = out / "doc" / "ffmpeg"
    if not ensure_shared_assets(doc_root_out, repo, logger):
        logger.warn(
            f"Skipping HTML doc for {target_version}: shared CSS unavailable"
        )
        return

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
        return

    _inject_ffmpeg_doc_footer(output_path, tag, logger)


def _inject_ffmpeg_doc_footer(output_path: Path, tag: str, logger: Logger) -> None:
    """Append the LGPL attribution footer to a rendered ``ffmpeg-all.html``.

    The page is produced by ``t2h.pm`` (makeinfo), not by our renderer, so the
    footer is spliced in by post-processing: inserted before the final
    ``</body>`` so it lands at the bottom of the document. A page missing
    ``</body>`` (unexpected) gets the footer appended and a warning.
    """
    info = THIRD_PARTY["ffmpeg"]
    footer = build_generated_doc_footer(
        project_title=info["title"],
        snapshot_label=f"tag {tag}",
        source_url=info["source_url"],
        license_name=info["license"],
        license_href=f"{DOC_TO_ROOT}/licenses/{info['license_file']}",
        notices_href=f"{DOC_TO_ROOT}/{NOTICES_FILENAME}",
        copyright_line=info["copyright"],
    )
    try:
        text = output_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warn(f"Could not read {output_path} to add license footer: {exc}")
        return

    marker = "</body>"
    idx = text.rfind(marker)
    if idx == -1:
        logger.warn(
            f"No </body> in {output_path}; appending license footer at end of file"
        )
        text = text + "\n" + footer
    else:
        text = text[:idx] + footer + text[idx:]
    output_path.write_text(text, encoding="utf-8")
