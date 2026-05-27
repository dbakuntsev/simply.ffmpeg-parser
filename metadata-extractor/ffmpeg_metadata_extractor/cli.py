import argparse
import os
from pathlib import Path

from .asset_check import check_assets
from .extractor import ExtractConfig, run_extraction


def _default_jobs() -> int:
    """Default parallelism: capped at 8 to keep git/disk contention bounded
    and to play nicely on shared CI runners. Falls back to 1 when
    ``os.cpu_count()`` is unavailable.
    """
    return min(8, os.cpu_count() or 1)


_ALL_CATEGORIES = {
    "options",
    "codecs",
    "filters",
    "demuxers",
    "muxers",
    "protocols",
    "bitstream_filters",
}


def _parse_categories(raw: str | None) -> set[str]:
    if not raw:
        return set(_ALL_CATEGORIES)
    categories = {c.strip().lower() for c in raw.split(",") if c.strip()}
    unknown = categories - _ALL_CATEGORIES
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown categories: {', '.join(sorted(unknown))}")
    if not categories:
        raise argparse.ArgumentTypeError("Categories list is empty")
    return categories


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ffmpeg-metadata-extract",
        description="Extract FFmpeg metadata for Simply.ffmpeg-parser",
    )
    parser.add_argument("--repo", required=True, help="Path to FFmpeg repository root")
    parser.add_argument(
        "--check-assets",
        dest="check_assets",
        nargs="?",
        const="latest",
        default=None,
        metavar="TAG",
        help=(
            "Compare vendored CSS assets (bootstrap.min.css, style.min.css) "
            "against an FFmpeg tag and exit. With no value, compares against "
            "the latest n<major>.<minor>.<patch> tag in --repo. Pass a tag "
            "(e.g. --check-assets n8.2.0) to compare against a specific one."
        ),
    )
    parser.add_argument("--tags", help="Comma-separated list of tags")
    parser.add_argument("--range", dest="tag_range", help="Tag range like n6.1.0..n7.1.2")
    parser.add_argument(
        "--latest-per-minor",
        dest="latest_per_minor",
        action="store_true",
        default=True,
        help="Keep only the highest patch tag per major.minor (default)",
    )
    parser.add_argument(
        "--no-latest-per-minor",
        dest="latest_per_minor",
        action="store_false",
        help="Keep all tags even if they map to the same major.minor",
    )
    parser.add_argument(
        "--out",
        help="Output root directory (required unless --check-assets is used)",
    )
    parser.add_argument(
        "--categories",
        help=(
            "Comma-separated subset of options,codecs,filters,demuxers,muxers,"
            "protocols,bitstream_filters"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue extracting other tags even if one fails",
    )
    parser.add_argument(
        "--no-worktree-fallback",
        dest="worktree_fallback",
        action="store_false",
        default=True,
        help="Disable temporary worktree fallback when git show cannot read files",
    )
    parser.add_argument(
        "--disable-html-doc",
        dest="html_doc",
        action="store_false",
        default=True,
        help="Skip generating ffmpeg-all.html via makeinfo --html",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=_default_jobs(),
        metavar="N",
        help=(
            "Number of worker processes used to extract distinct tags in "
            "parallel. Defaults to min(8, cpu_count). Set to 1 for the "
            "historical sequential behavior."
        ),
    )
    parser.add_argument(
        "--x264-repo",
        dest="x264_repo",
        metavar="PATH",
        help=(
            "Optional path to an upstream x264 git checkout. When set, "
            "the extractor parses x264.c's verbose help text to fill in "
            "valid values + descriptions for libx264's -preset / -tune / "
            "-profile (which FFmpeg declares as plain strings and passes "
            "through to x264, so the AVOption-array parser sees no "
            "enumerated values for them)."
        ),
    )
    parser.add_argument(
        "--x265-repo",
        dest="x265_repo",
        metavar="PATH",
        help=(
            "Optional path to an upstream x265 git checkout. Like "
            "--x264-repo but for libx265's -preset / -tune / -profile. "
            "Snapshot is pinned to the most recent stable release tag "
            "(matching \\d+\\.\\d+(\\.\\d+)?) at or before the FFmpeg tag's "
            "release date, so older FFmpeg bundles get an "
            "approximately-contemporary x265 view."
        ),
    )

    args = parser.parse_args()

    if args.check_assets is not None:
        return check_assets(Path(args.repo), args.check_assets)

    if not args.out:
        parser.error("--out is required (unless --check-assets is used)")

    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    tag_range = None
    if args.tag_range:
        if ".." not in args.tag_range:
            parser.error("--range must be in the form <from..to>")
        start, end = args.tag_range.split("..", 1)
        tag_range = (start.strip(), end.strip())

    categories = _parse_categories(args.categories)

    jobs = max(1, args.jobs)
    x264_repo = Path(args.x264_repo) if args.x264_repo else None
    x265_repo = Path(args.x265_repo) if args.x265_repo else None

    config = ExtractConfig(
        repo=Path(args.repo),
        out=Path(args.out),
        tags=tags,
        tag_range=tag_range,
        latest_per_minor=args.latest_per_minor,
        categories=categories,
        verbose=args.verbose,
        continue_on_error=args.continue_on_error,
        worktree_fallback=args.worktree_fallback,
        html_doc=args.html_doc,
        jobs=jobs,
        x264_repo=x264_repo,
        x265_repo=x265_repo,
    )

    return run_extraction(config)


if __name__ == "__main__":
    raise SystemExit(main())
