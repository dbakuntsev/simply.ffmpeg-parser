from __future__ import annotations

import contextlib
import re
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


def commit_at_or_before(repo: Path, iso_date: str, branch: str = "HEAD") -> str | None:
    """Return the SHA of the most recent commit on ``branch`` whose
    committer date is ``<= iso_date``, or ``None`` if no such commit
    exists (i.e. the branch's first commit is newer than the date).

    Used to pin an external repository — e.g. x264 — to an
    approximately-contemporary snapshot for each FFmpeg release tag.
    ``iso_date`` should be a string git understands (RFC 3339 / ISO 8601);
    the output of :func:`tag_date_iso` works directly.
    """
    try:
        result = run_git(
            repo,
            ["log", "-1", f"--before={iso_date}", "--format=%H", branch],
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    return value or None


def tag_at_or_before(
    repo: Path,
    iso_date: str,
    name_pattern: "re.Pattern[str] | None" = None,
) -> str | None:
    """Return the most recent tag (by committer date of the commit it
    points at) whose date is ``<= iso_date``, or ``None``.

    When ``name_pattern`` is supplied, only tags whose short name fully
    matches it are considered. The common use is filtering out pre-
    release tags (e.g. ``3.5_RC1``) when picking an "approximately
    contemporary release" for a date-pinned snapshot.

    Compared to :func:`commit_at_or_before`, this is the right tool for
    repos that DO publish release tags (x265, libaom, libsvtav1, …) —
    the tag name carries semantic version info you can surface in logs,
    and tag boundaries align with what library users actually shipped.
    """
    # ``committerdate`` is the field on the *commit* object; for ANNOTATED
    # tags (a tag object that wraps the commit) it comes back empty unless
    # we prefix it with ``*`` (the deref operator) to follow the tag to
    # its commit. Lightweight tags point directly at a commit, so the
    # plain field works for them and ``*committerdate`` comes back empty.
    # Most repos mix both styles (x265 does — older 0.x/1.x/2.x/3.x tags
    # are lightweight, 3.4.1+/4.x are annotated). Asking for both and
    # taking the non-empty one is the only reliable shape.
    try:
        result = run_git(
            repo,
            [
                "for-each-ref",
                "--format=%(refname:short)|%(committerdate:iso-strict)"
                "|%(*committerdate:iso-strict)",
                "refs/tags/",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    best_name: str | None = None
    best_date: str = ""
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        date = (parts[1].strip() or parts[2].strip())
        if not name or not date:
            continue
        if name_pattern is not None and not name_pattern.fullmatch(name):
            continue
        if date > iso_date:
            continue
        # Lexicographic compare works because both are ISO-8601 with same
        # offset width — newer date string sorts higher.
        if date > best_date:
            best_date = date
            best_name = name
    return best_name


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
