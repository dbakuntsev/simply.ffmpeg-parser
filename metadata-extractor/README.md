# FFmpeg Metadata Extractor

CLI utility for generating FFmpeg metadata bundles used by the Simply.ffmpeg-parser SPA.

## Usage

```bash
ffmpeg-metadata-extract --repo /path/to/ffmpeg --out /path/to/output
```

## Examples

```bash
ffmpeg-metadata-extract --repo /repos/ffmpeg --tags n7.1.2 --out ./dist
ffmpeg-metadata-extract --repo /repos/ffmpeg --range n6.1.0..n7.1.2 --out ./dist
```

## HTML documentation

In addition to the JSON metadata under `<out>/metadata/ffmpeg/<version>/`, the
extractor renders a single-page HTML reference (`ffmpeg-all.html`) per version
under `<out>/doc/ffmpeg/<version>/`, generated from each tag's `doc/ffmpeg.texi`
via GNU `makeinfo --html --no-split`. The synthetic `doc/config.texi` we
already write into the staged docs forces `config-all`, so the rendered page
includes the complete reference (options, codecs, filters, formats, …) for
that version. Pass `--disable-html-doc` to skip this step.

The two CSS files (`bootstrap.min.css`, `style.min.css`) referenced by every
generated page live **once** at `<out>/doc/ffmpeg/`, shared by all versions.
Each per-version HTML links to them via `../bootstrap.min.css` and
`../style.min.css`.

## Vendored assets (`ffmpeg_metadata_extractor/assets/`)

Three files are vendored from upstream FFmpeg and shipped with this package:

| File                | Origin                                          | Purpose                                  |
|---------------------|-------------------------------------------------|------------------------------------------|
| `bootstrap.min.css` | FFmpeg tag **n8.1.1**, `doc/bootstrap.min.css`  | Layout/typography for rendered HTML doc. |
| `style.min.css`     | FFmpeg tag **n8.1.1**, `doc/style.min.css`      | FFmpeg site styling for rendered HTML.   |
| `t2h.pm`            | FFmpeg tag **n8.1.1**, `doc/t2h.pm` (modified)  | `makeinfo --html` init file (theme).     |

### Why these were vendored

- **`t2h.pm`** — FFmpeg releases up to ~n7.x call removed Texinfo 6.x APIs
  (`$self->gdt(...)`) directly. With a modern `makeinfo` (Texinfo 7.1+) the
  original `t2h.pm` from those tags fails with
  `Can't locate object method "gdt" via package "Texinfo::Convert::HTML"`,
  and makeinfo silently produces no output. The n8.x `t2h.pm` is version-gated
  (`$program_version_num >= 7.001090 ? cdt(...) : gdt(...)`) and works against
  every Texinfo release we care about. Substituting it is safe because
  `t2h.pm` is purely presentational: it controls heading levels, the
  `<head>` block, TOC placement, and a few formatting callbacks — never the
  documented content.
- **`bootstrap.min.css` and `style.min.css`** — both are referenced by
  `t2h.pm`'s `<head>` block and are required for the page to look correct.
  Spot-checks against the FFmpeg repo showed `bootstrap.min.css` is byte-
  identical from n5.1 through n8.1, and `style.min.css` is identical from
  n5.1 through n7.1 with the n8.1.1 file being a strict superset (it appends
  two extra rules used only by n8.x docs). A single shared copy of the
  n8.1.1 version therefore styles every version correctly, and keeps the
  shared assets out of every per-version directory.

### How `t2h.pm` was modified

Exactly two lines were edited from the upstream n8.1.1 file. The default
emits CSS hrefs relative to the HTML file:

```perl
    <link rel="stylesheet" type="text/css" href="bootstrap.min.css">
    <link rel="stylesheet" type="text/css" href="style.min.css">
```

These were changed to reference the shared parent directory:

```perl
    <link rel="stylesheet" type="text/css" href="../bootstrap.min.css">
    <link rel="stylesheet" type="text/css" href="../style.min.css">
```

No other edits. The change lives inside the `$head2` here-doc literal
starting around line 303 of the file (search for `bootstrap.min.css`).

### Refreshing the assets from a newer FFmpeg release

When upstream FFmpeg cuts a new release that updates these files, follow this
procedure to refresh the vendored copies. The `--check-assets` command (see
below) is meant to detect when this needs to happen and assess the risk.

1. **Pick a reference tag.** Usually the latest stable `n<major>.<minor>.0`
   or `.<patch>`, e.g. `n8.2.0`.
2. **Run the asset check first** to see what changed:

   ```bash
   ffmpeg-metadata-extract --repo /repos/ffmpeg --check-assets n8.2.0
   ```

   - `identical` → nothing to do for that file.
   - `differs` + *upstream is a strict superset* → safe refresh; upstream
     added rules without modifying existing ones.
   - `differs` + *not a superset* → diff manually before replacing; existing
     rules may have changed or been removed.
3. **Copy the new bytes** from the reference tag into the assets directory:

   ```bash
   git -C /repos/ffmpeg show n8.2.0:doc/bootstrap.min.css \
       > metadata-extractor/ffmpeg_metadata_extractor/assets/bootstrap.min.css
   git -C /repos/ffmpeg show n8.2.0:doc/style.min.css \
       > metadata-extractor/ffmpeg_metadata_extractor/assets/style.min.css
   git -C /repos/ffmpeg show n8.2.0:doc/t2h.pm \
       > metadata-extractor/ffmpeg_metadata_extractor/assets/t2h.pm
   ```
4. **Re-apply the `t2h.pm` modification** described above (two `href`
   changes inside the `$head2` here-doc). Without this, generated HTML
   pages will look for CSS in their own directory and render unstyled.
5. **Update this README's "Origin" column** to the new tag.
6. **Regenerate a few versions** end-to-end and spot-check the rendered HTML
   in a browser to confirm styling still works for both old and new tags.

## Checking vendored assets against upstream

`--check-assets [TAG]` compares the vendored CSS files against the same files
in an FFmpeg checkout, without running an extraction.

```bash
# Compare against the latest n<major>.<minor>.<patch> tag in --repo
ffmpeg-metadata-extract --repo /repos/ffmpeg --check-assets

# Compare against a specific tag
ffmpeg-metadata-extract --repo /repos/ffmpeg --check-assets n8.2.0
```

For each asset, the command reports:

- **identical** — vendored bytes match upstream.
- **differs, upstream is a strict superset** — every byte of the vendored
  copy appears verbatim inside the upstream copy. Upstream only added
  content; a refresh will not remove or change existing rules. Low risk.
- **differs, NOT a superset** — upstream has removed or modified content
  the vendored copy contains. Review the diff before refreshing; the
  generated HTML may depend on rules that no longer exist upstream.

Exit codes: `0` all identical, `1` repo/tag lookup failed, `4` at least one
asset differs (regardless of superset relationship).

`t2h.pm` is deliberately **not** checked — it carries a local modification
(the two CSS href rewrites), so a byte comparison would always report a
diff. Refreshing it is a manual three-step process: copy from upstream,
re-apply the two `href` edits, verify the file still loads with the
installed Texinfo by running an extraction against an older tag.
