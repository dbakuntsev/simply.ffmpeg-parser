# FFmpeg Metadata Extractor

A Python CLI that produces the per-version JSON metadata bundles and
single-page HTML reference docs that the [Simply FFmpeg Parser](README.md)
SPA consumes. It takes an FFmpeg git checkout as input, processes one or
more release tags, and writes everything into a directory tree the SPA
can serve as static assets.

The extractor never runs `ffmpeg` or `./configure`. It works strictly from
the Texinfo documentation and the `libav*` C source files committed in each
release tag, so it can be re-run for historical versions years after
release.

- Source: [`metadata-extractor/`](metadata-extractor/)
- In-package README (build-time HTML asset details, pinned tag):
  [`metadata-extractor/README.md`](metadata-extractor/README.md)

## What it extracts

For each selected tag, the extractor emits one JSON file per category under
`<out>/metadata/ffmpeg/<major.minor>/`:

| File | Source(s) | Contents |
|------|-----------|----------|
| `options.json` | `doc/ffmpeg.texi` (falling back to `doc/ffmpeg-all.texi`, then `doc/ffmpeg-opt.texi`) **+** `fftools/ffmpeg_opt.c`, `fftools/opt_common.h` / `fftools/cmdutils.h` | Every documented CLI option: name, aliases, scope (global/input/output), valueType, accepted enum values, dependencies, conflicts, description, doc anchor. The `fftools/` C sources are scanned to fold in short/legacy aliases (e.g. `-apre`/`-vpre`/`-spre` for `-pre`, `-stag` for `-tag`) and to synthesize entries for fully-undocumented options (e.g. `-hwaccel_output_format`) |
| `codecs.json` | `doc/codecs.texi` + `libavcodec/allcodecs.c` (or `codec_list.c`) | Codec name, type (video/audio/subtitle), aliases, encoder/decoder flags |
| `filters.json` | `doc/filters.texi` | Filter name, type, aliases, per-filter parameters and per-arg descriptions |
| `muxers.json` | `doc/muxers.texi` **+** `doc/outdevs.texi` | Muxer name, aliases, options. Output devices are merged in (libavdevice surfaces them as muxers at runtime), so `-f dshow`, `-f sdl`, etc. resolve |
| `demuxers.json` | `doc/demuxers.texi` **+** `doc/indevs.texi` | Demuxer name, aliases, options. Input devices are merged in the same way, so `-f lavfi`, `-f gdigrab`, etc. resolve |
| `protocols.json` | `doc/protocols.texi` | Protocol name, options |
| `bitstream_filters.json` | `doc/bitstream_filters.texi` | Bitstream filter name, options |

Output entries inside each file are deduped by lowercase canonical name and
sorted alphabetically so diffs between versions stay stable. Pass
`--categories options,codecs,filters` to limit which categories are
extracted.

In addition to the JSON, the extractor renders a single-page HTML reference
(`ffmpeg-all.html`) per version under `<out>/doc/ffmpeg/<version>/`. The SPA
links into this from the inspector drawer so users can jump to the upstream
documentation entry for the exact version they're inspecting.

## How it does it

### Tag selection

Tags are filtered to `^n(\d+)\.(\d+)\.(\d+)$` — any non-matching tag is
ignored. From there:

- `--tags n7.1.2,n8.0.0` pins specific tags.
- `--range n6.1.0..n7.1.2` selects an inclusive range.
- With neither flag, every matching tag is processed.
- `--latest-per-minor` (**on by default**) collapses each `major.minor`
  group down to its highest patch tag. Pass `--no-latest-per-minor` to keep
  every patch.

### Reading files out of a tag

The extractor avoids touching the working tree:

1. **`git archive <tag> doc`** streams the `doc/` subtree into a tempdir.
   The `libav*` C source files we need are read via `git show <tag>:<path>`.
2. On failure, it falls back to `git worktree add --detach` in a tempdir
   (cleaned up in a `finally`) unless `--no-worktree-fallback` is set.

This way, multiple tags can be processed against a single shared FFmpeg
clone without ever mutating the checkout.

### Parsing Texinfo via `makeinfo --xml`

Rather than scraping raw `.texi` files, the extractor invokes
`makeinfo --xml` on each documentation file and parses the resulting XML.
This gives stable, structured access to sections, `@table` blocks,
`@item`s, anchors, and cross-references — independent of how the upstream
docs format their prose.

`makeinfo` will refuse to render `doc/ffmpeg.texi` without a `config.texi`
(the build-time-generated file that lists which features are compiled in).
The extractor synthesizes one by parsing `configure` and emitting a
features-everything-enabled stand-in into the staged `doc/` directory
before invoking `makeinfo`. This is what lets `makeinfo` resolve
`@include config.texi` and evaluate every `@ifset config-…` conditional
without needing to actually run `./configure`.

### Two-source merge for codecs

Codec metadata comes from two places:

- `doc/codecs.texi` provides the **documented** codecs: name, type
  (video/audio/subtitle), and aliases.
- `libavcodec/allcodecs.c` (or `codec_list.c` on newer trees) is scanned
  for `ff_<name>_(encoder|decoder)` symbols to OR in encoder/decoder
  capability flags.

Codecs that only appear in the C source (undocumented) are emitted with
`type: "video"` as a default — the extractor does not try to infer type
from the symbol name. If `codecs.texi` is missing but the C file exists,
all codecs default to `type: "video"` and a warning is logged.

### Augmenting `options.json` from the `fftools/` C sources

The CLI option Texinfo (`doc/ffmpeg.texi` & friends) is a curated subset of
FFmpeg's actual `OptionDef options[]` table. To close the gap, the extractor
also parses `fftools/ffmpeg_opt.c` and `fftools/opt_common.h`
(`fftools/cmdutils.h` on older tags) and folds two pieces of information into
the doc-derived option list:

- **Aliases for documented options.** Short/legacy names that share a backing
  handler with a documented canonical (`apre`/`vpre`/`spre` for `pre`, `stag`
  for `tag`, `scodec`/`dcodec` for `codec`, `lavfi` for `filter_complex` on
  older tags, …) are attached to the canonical's `aliases` list.
- **Top-level entries for fully-undocumented options.** Names in the C table
  that are neither documented nor folded in as an alias get a synthesized
  entry whose `scope` / `valueType` / `signature` are inferred from the
  `OPT_*` flag tokens on the row, and whose `description` is the row's short
  C help string. `-hwaccel_output_format` is the canonical example.

Both passes are gap-fill only: a name that has a doc-derived entry always
keeps its richer description. If every `fftools/` source fails to fetch for
a given tag, augmentation is silently skipped and the doc-derived list is
emitted unchanged.

### Devices folded into demuxers/muxers

FFmpeg's `-f <name>` is a single flag that selects either a (de)muxer or a
device — at runtime libavdevice surfaces input devices through the demuxer
API and output devices through the muxer API. Treating devices as separate
output categories would force the SPA to special-case `-f lavfi`, `-f dshow`,
`-f gdigrab`, etc. Instead, `doc/indevs.texi` is parsed and merged into
`demuxers.json`, and `doc/outdevs.texi` into `muxers.json`. On name
collisions (e.g. `fbdev` is documented as both an indev and an outdev), the
demuxer/muxer entry wins; device-only entries land with an empty `options`
list. The SPA's `unknown-demuxer` / `unknown-muxer` diagnostics rely on this
merge.

## How HTML documentation is built

Each version's `ffmpeg-all.html` is generated by GNU
`makeinfo --html --no-split` against the same staged `doc/ffmpeg.texi`.
The synthetic `config.texi` (above) forces `config-all`, so the rendered
page includes every option, codec, filter, format, and protocol available
in that version. Two CSS files (`bootstrap.min.css`, `style.min.css`) land
once under `<out>/doc/ffmpeg/`, shared across every version.

The renderer also needs a Perl init file (`t2h.pm`) plus the two CSS files
themselves. None of those are committed — they are **fetched from `--repo`
at build time** via `git show <tag>:doc/<file>` against a pinned FFmpeg tag,
which keeps this repo 100% MIT (`t2h.pm` is GPLv3+). The pinned tag must be
present in `--repo`; if it is absent, HTML is skipped per version with a
warning while JSON extraction continues. Pass `--disable-html-doc` to skip
HTML entirely.

The *why* behind the pinned tag (a Texinfo 7.1+ API change that makes older
`t2h.pm` copies silently fail), the exact `href` repoints applied in code to
share one CSS pair across versions, and the recipe for moving to a newer
pinned tag are all documented in the in-package README:
[`metadata-extractor/README.md`](metadata-extractor/README.md).

## Third-party license attribution

The JSON bundles and HTML reference pages are derivative works of FFmpeg
(LGPL v2.1+), x264 (GPL v2+) and x265 (GPL v2+). On every run the extractor
emits the attribution required to redistribute them:

- `<out>/licenses/LICENSE_FFMPEG.txt`, `LICENSE_X264.txt`, `LICENSE_X265.txt` —
  each upstream's verbatim license text, fetched at build time (`COPYING.LGPLv2.1`
  from FFmpeg's pinned **n8.1.1** tag; `COPYING` from `HEAD` of the x264/x265
  clones). Like `t2h.pm`/CSS, these are never vendored, so the repo stays MIT.
- `<out>/THIRD-PARTY-NOTICES.html` — an aggregate notices page listing each
  upstream, its license, copyright, the snapshots included in this run, and a
  link to the corresponding source. Built once at the end of the run by scanning
  the output tree.
- A footer on every rendered reference page (`ffmpeg-all.html`,
  `x264-reference.html`, `x265-reference.html`) marking it as generated
  documentation and linking the bundled license text + the corresponding source
  at the exact tag/commit.

These outputs (plus the per-version directories) are gitignored. See
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md) for the full model.

## Running it locally

### Prerequisites

- **Python ≥3.10** (the package has no runtime Python dependencies)
- **git** on `PATH`
- **GNU `makeinfo`** (from `texinfo` ≥ 7.1) on `PATH` — required for both
  XML parsing and HTML rendering
- An **FFmpeg git checkout** to point `--repo` at:
  ```bash
  git clone https://github.com/FFmpeg/FFmpeg.git /path/to/ffmpeg
  ```

On Debian/Ubuntu:

```bash
sudo apt-get install -y git texinfo
```

On macOS (Homebrew): `brew install texinfo` (and put
`$(brew --prefix texinfo)/bin` on your `PATH`, since macOS ships an older
system `makeinfo`).

On Windows (MSYS2): install [MSYS2](https://www.msys2.org/), then from the
MSYS2 shell:

```bash
pacman -Syu                       # update package DB (may require restart)
pacman -S git texinfo perl tar    # makeinfo + git + Perl runtime + tar
```

Under MSYS2 both `makeinfo` and `texi2any` are Perl scripts, so the MSYS2
Perl is required at runtime — `pacman -S texinfo` pulls it in as a
dependency, listed above explicitly for clarity. To run
`ffmpeg-metadata-extract` from a regular `cmd.exe` / PowerShell session
rather than from the MSYS2 shell, put `C:\msys64\usr\bin` on your `PATH`
(adjust if you installed MSYS2 somewhere else) so Windows can find
`makeinfo`, `perl`, `tar`, and `git` from outside MSYS2.

### Installing

From the repo root, install the package in editable mode so the
`ffmpeg-metadata-extract` console script is on your `PATH`:

```bash
python -m pip install --upgrade pip
pip install -e ./metadata-extractor
```

A virtualenv is recommended but not required.

### Generating bundles for the SPA

To regenerate everything the SPA needs against a local FFmpeg checkout:

```bash
ffmpeg-metadata-extract \
    --repo /path/to/ffmpeg \
    --out web/public \
    --range n3.4.0..n9999.9999.9999 \
    --latest-per-minor \
    --verbose
```

This writes:

- `web/public/metadata/ffmpeg/<version>/*.json` (one directory per kept tag)
- `web/public/doc/ffmpeg/<version>/ffmpeg-all.html` plus the shared
  `bootstrap.min.css` and `style.min.css` one level up

After running the extractor you still need to regenerate
`web/public/metadata/ffmpeg/index.json` — the SPA's version list — using
the same logic the deploy workflow uses (see below).

### A single version, quickly

```bash
ffmpeg-metadata-extract --repo /path/to/ffmpeg --tags n7.1.2 --out ./dist
```

### Exit codes

- `0` — success
- `1` — missing or invalid `--repo`
- `2` — tag selection failed (range yielded nothing, pinned tag not found,
  invalid range syntax, …)
- `3` — extraction failed for at least one tag. Pair with
  `--continue-on-error` to collect failures instead of bailing on the first.

## How it's used in the GitHub Actions deploy workflow

[`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml)
runs on every published GitHub Release (and on manual
`workflow_dispatch`). The relevant steps:

1. **Install system packages** — `git` and `texinfo` from apt.
2. **Set up Python 3.12 and Node.js 20** with npm cache.
3. **Install the extractor** — `pip install -e ./metadata-extractor` so
   the `ffmpeg-metadata-extract` console script is on `PATH`.
4. **Restore the FFmpeg clone cache** keyed by `github.run_id`, falling
   back to any older `ffmpeg-git-*` key. The clone lives at
   `${{ github.workspace }}/.ffmpeg-src`.
5. **Clone or update FFmpeg** from `https://github.com/FFmpeg/FFmpeg.git`,
   fetching tags. If the cache hit, the existing clone is just `git fetch`ed.
6. **Run the extractor** over `n3.4.0..n9999.9999.9999`,
   `--latest-per-minor`, output into `web/public`.
7. **Regenerate `metadata/ffmpeg/index.json`** with a small inline Python
   script — this is where the cache-buster tokens are computed (next
   section).
8. **`npm ci` + `npm run build`** under `web/`.
9. **Upload the Pages artifact** from `web/dist` and deploy it via
   `actions/deploy-pages@v4` in a second job.

### The cache buster

`index.json` is regenerated post-extraction with the shape:

```json
{
  "versions": ["3.4", "4.0", "…", "8.1"],
  "tokens": {
    "8.1": {
      "metadata": { "options.json": "ab12cd34ef56", "codecs.json": "…", … },
      "doc":      { "ffmpeg-all.html": "…" }
    },
    …
  }
}
```

For every JSON file in `web/public/metadata/ffmpeg/<version>/` and every
HTML file in `web/public/doc/ffmpeg/<version>/`, the workflow computes a
SHA-256 of the file's bytes and keeps the first 12 hex characters as the
token. The SPA appends these tokens as a `?v=<token>` query string when
fetching each per-version asset.

**Why this is here:** the metadata index lives at a stable URL
(`metadata/ffmpeg/index.json`) and is fetched cache-busted on every load.
Once the SPA has the index, the cache-buster tokens give it
content-addressed URLs for the per-version metadata and HTML, so:

- A version's bundle that did not change between two deploys keeps the
  same token and stays in the browser cache.
- A version's bundle that did change (e.g. when bumping a PATCH tag and
  rolling it up into its `major.minor` directory) gets a new token, so the
  browser bypasses the cache and re-fetches it.

This is what lets the deploy workflow safely overwrite an existing
`<major.minor>/` directory with newer-patch content without users seeing
stale data and without invalidating caches for unrelated versions.
