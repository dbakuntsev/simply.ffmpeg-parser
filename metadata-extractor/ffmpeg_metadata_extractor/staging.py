"""Materialize a tag's ``doc/`` (plus libav* sources) into a temp dir
without touching the working tree, and synthesize the ``config.texi``
that makeinfo needs to resolve ``@include`` references.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .config_texi import generate_config_texi
from .git_utils import show_file
from .models import ExtractionError


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
def staged_doc(repo: Path, tag: str, version: str, fallback_root: Path | None):
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
