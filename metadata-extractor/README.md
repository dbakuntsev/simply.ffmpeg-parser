# FFmpeg Metadata Extractor

CLI utility for generating FFmpeg metadata bundles used by the Simply.ffmpeg-parser SPA.

For the user-facing overview — installation, what gets extracted into each
JSON bundle (options, codecs, filters, muxers/demuxers including devices,
protocols, bitstream filters), how the deploy workflow wires it up, and the
cache-buster scheme — see [`../METADATA_EXTRACTOR.md`](../METADATA_EXTRACTOR.md).
This file focuses on the build-time HTML rendering pipeline (pinned-tag fetch
of `t2h.pm` + CSS, the `href` repoints) since that is the part with
non-obvious mechanics worth documenting in-package.

## Usage

```bash
ffmpeg-metadata-extract --repo /path/to/ffmpeg --out /path/to/output
```

## Examples

```bash
ffmpeg-metadata-extract --repo /repos/ffmpeg --tags n7.1.2 --out ./dist
ffmpeg-metadata-extract --repo /repos/ffmpeg --range n6.1.0..n7.1.2 --out ./dist
```

## HTML rendering assets (fetched at build time)

> The top-level [`../METADATA_EXTRACTOR.md`](../METADATA_EXTRACTOR.md)
> covers *what* the HTML rendering step produces (`ffmpeg-all.html` per
> version under `<out>/doc/ffmpeg/`, shared CSS one level up). This
> section is the *why* and *how* — the part a future maintainer needs
> when bumping the pinned tag or debugging a silent makeinfo failure.

The HTML renderer needs three files from FFmpeg's `doc/` tree:

| File                | Source                                            | Purpose                                  |
|---------------------|---------------------------------------------------|------------------------------------------|
| `bootstrap.min.css` | `doc/bootstrap.min.css` at the pinned tag         | Layout/typography for rendered HTML doc. |
| `style.min.css`     | `doc/style.min.css` at the pinned tag             | FFmpeg site styling for rendered HTML.   |
| `t2h.pm`            | `doc/t2h.pm` at the pinned tag (modified in code) | `makeinfo --html` init file (theme).     |

None of these are committed to this repo. They are fetched from `--repo` at
build time via `git show <tag>:doc/<file>` (`_pinned_asset_bytes` in
`extractor.py`) and written into the staged docs / output. This keeps the
package **100% MIT**: `t2h.pm` carries an FFmpeg GPLv3+ header, and the project
rule is that GPL-derived artifacts are generated at build time and never
checked in.

### The pinned tag

The tag is pinned (`_PINNED_ASSET_TAG = "n8.1.1"` in `extractor.py`), **not**
"the tag being rendered". FFmpeg releases up to ~n7.x ship a `t2h.pm` that
calls removed Texinfo 6.x APIs (`$self->gdt(...)`); under a modern `makeinfo`
(Texinfo 7.1+) those fail with
`Can't locate object method "gdt" via package "Texinfo::Convert::HTML"` and
makeinfo silently produces no output. The n8.1.1 `t2h.pm` is version-gated
(`$program_version_num >= 7.001090 ? cdt(...) : gdt(...)`) and renders across
the full tag range. Substitution is safe because `t2h.pm` is purely
presentational — heading levels, the `<head>` block, TOC placement, formatting
callbacks — never the documented content. The n8.1.1 CSS pair styles every
version correctly (spot-checks showed `bootstrap.min.css` byte-identical n5.1→n8.1,
and `style.min.css` a strict superset of the older copies).

**`--repo` must contain the `n8.1.1` tag** for HTML rendering. A full clone
(as CI uses) always has it. If it is absent, HTML generation is skipped with a
loud warning per version; JSON extraction is unaffected. Pass
`--disable-html-doc` to skip HTML entirely.

### The `t2h.pm` modification

The only edit to the upstream file is two `href` rewrites, applied in code
(`_T2H_HREF_REPOINTS` in `extractor.py`) so the modification lives as MIT
source rather than a committed GPL derivative. The upstream `$head2` here-doc
emits CSS relative to the HTML file:

```perl
    <link rel="stylesheet" type="text/css" href="bootstrap.min.css">
    <link rel="stylesheet" type="text/css" href="style.min.css">
```

Each is repointed one directory up (`href="../bootstrap.min.css"` etc.) so the
shared CSS at `<out>/doc/ffmpeg/` resolves from `<out>/doc/ffmpeg/<version>/`.
Each substitution must match exactly once; a miss (only possible if the pinned
tag is changed to one whose `t2h.pm` differs) skips HTML with a loud warning
naming the unmatched href.

### Moving to a newer pinned tag

If a future Texinfo or FFmpeg release requires a newer init file, bump
`_PINNED_ASSET_TAG` in `extractor.py`, confirm that tag's `doc/t2h.pm` still
contains the two `href="bootstrap.min.css"` / `href="style.min.css"` lines
(update `_T2H_HREF_REPOINTS` if not), and regenerate a few old + new versions
end-to-end to confirm styling still works.
