"""Compare vendored CSS assets against a reference FFmpeg tag.

The extractor ships ``bootstrap.min.css`` and ``style.min.css`` in
``assets/`` so all generated docs can share a single copy. When upstream
FFmpeg updates these files, we need a way to detect drift and decide
whether to refresh the vendored copies.

This module powers the ``--check-assets`` CLI switch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .extractor import _ASSETS_DIR, _SHARED_CSS_FILES, parse_tag_version, semver_key
from .git_utils import list_tags, show_file_bytes


def _select_reference_tag(repo: Path, requested: str) -> str:
    """Resolve the ``--check-assets`` value to a concrete tag name.

    ``requested == "latest"`` returns the highest ``n<major>.<minor>.<patch>``
    tag in ``repo``. Any other value is treated as a literal tag name and
    validated against the tag list.
    """
    tags = [t for t in list_tags(repo) if parse_tag_version(t)]
    if not tags:
        raise ValueError("Repository contains no n<major>.<minor>.<patch> tags")

    if requested == "latest":
        return max(tags, key=semver_key)

    if requested not in tags:
        raise ValueError(f"Tag not found in repository: {requested}")
    return requested


def _sha256_short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def check_assets(repo: Path, requested: str) -> int:
    """Compare each shared CSS asset against ``repo`` at the resolved tag.

    For each asset, reports identical/differs and -- when different -- whether
    the upstream copy is a strict superset of the vendored copy (i.e. the
    vendored bytes appear verbatim inside the upstream bytes). A superset
    upgrade is low-risk: every rule we already ship is still present.

    Returns exit codes: 0 = all identical, 4 = at least one differs,
    1 = repo or tag lookup failed.
    """
    if not (repo / ".git").exists():
        print(f"ERROR: not a git repository: {repo}")
        return 1

    try:
        tag = _select_reference_tag(repo, requested)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Checking vendored CSS assets against {tag} (doc/)")
    print("-" * 60)

    any_diff = False
    for name in _SHARED_CSS_FILES:
        vendored = (_ASSETS_DIR / name).read_bytes()
        upstream = show_file_bytes(repo, tag, f"doc/{name}")

        if upstream is None:
            print(f"  {name}: not present in {tag}")
            any_diff = True
            continue

        if upstream == vendored:
            print(
                f"  {name}: identical "
                f"(sha256:{_sha256_short(vendored)}, {len(vendored)} B)"
            )
            continue

        any_diff = True
        print(f"  {name}: differs")
        print(
            f"    vendored: sha256:{_sha256_short(vendored)}, "
            f"{len(vendored)} B"
        )
        print(
            f"    upstream: sha256:{_sha256_short(upstream)}, "
            f"{len(upstream)} B"
        )
        if vendored in upstream:
            print(
                "    upstream is a strict superset of vendored -- "
                "refreshing is low-risk (every existing rule is preserved)."
            )
        else:
            print(
                "    upstream is NOT a superset -- refreshing may remove or "
                "alter rules the generated HTML relies on. Diff manually "
                "before updating."
            )

    return 4 if any_diff else 0
