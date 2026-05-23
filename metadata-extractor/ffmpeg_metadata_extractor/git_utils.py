from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def list_tags(repo: Path) -> list[str]:
    result = run_git(repo, ["tag", "-l"], check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tag_date_iso(repo: Path, tag: str) -> str | None:
    try:
        result = run_git(repo, ["log", "-1", "--format=%cI", tag], check=True)
    except subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    return value or None


def show_file(repo: Path, tag: str, path: str) -> str | None:
    try:
        result = run_git(repo, ["show", f"{tag}:{path}"])
    except subprocess.CalledProcessError:
        return None
    return result.stdout


def show_file_bytes(repo: Path, tag: str, path: str) -> bytes | None:
    """Return the raw bytes of ``path`` at ``tag``, or ``None`` if not present.

    Used for byte-exact comparisons (e.g. checksum/superset checks against
    vendored binary or whitespace-sensitive assets). ``show_file`` decodes
    via the locale and may rewrite line endings on Windows.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{tag}:{path}"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return result.stdout


@contextlib.contextmanager
def temporary_worktree(repo: Path, tag: str) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="ffmpeg-worktree-"))
    try:
        run_git(repo, ["worktree", "add", "--detach", str(temp_dir), tag], check=True)
        yield temp_dir
    finally:
        try:
            run_git(repo, ["worktree", "remove", "--force", str(temp_dir)], check=False)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
