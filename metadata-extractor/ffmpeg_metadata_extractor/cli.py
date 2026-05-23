import argparse
from pathlib import Path

from .asset_check import check_assets
from .extractor import ExtractConfig, run_extraction


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
    )

    return run_extraction(config)


if __name__ == "__main__":
    raise SystemExit(main())
