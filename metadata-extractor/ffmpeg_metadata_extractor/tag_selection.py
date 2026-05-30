"""Tag discovery and filtering for the FFmpeg repo.

Produces the ordered list of tags ``run_extraction`` will iterate over,
optionally collapsed to the latest patch per major.minor.
"""

from __future__ import annotations

import re

from ._logging import Logger
from .git_utils import list_tags
from .models import ExtractConfig, ExtractionError

_TAG_PATTERN = re.compile(r"^n(\d+)\.(\d+)\.(\d+)$")


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
