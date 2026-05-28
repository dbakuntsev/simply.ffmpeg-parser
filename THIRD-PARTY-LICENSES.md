# Third-party licenses & attribution

The source code in this repository (`web/` and `metadata-extractor/`) is
licensed under the [MIT License](LICENSE).

The **artifacts the project deploys to GitHub Pages are not MIT.** The
metadata extractor generates, and the SPA serves, files that are *derivative
works* of upstream projects and are therefore distributed under each upstream's
own license:

| Deployed artifact | Derived from | License |
|---|---|---|
| `metadata/ffmpeg/<ver>/*.json` | FFmpeg documentation + `libav*` headers | LGPL v2.1 or later |
| `doc/ffmpeg/<ver>/ffmpeg-all.html` | FFmpeg documentation (rendered with FFmpeg's `doc/t2h.pm`) | LGPL v2.1 or later |
| `doc/ffmpeg/{bootstrap,style}.min.css` | FFmpeg `doc/` (verbatim) | MIT (own notices intact) |
| `doc/x264/<commit>/x264-reference.html` | x264 command-line help + source | GPL v2 or later |
| `doc/x265/<commit>/x265-reference.html` | x265 command-line help + source | GPL v2 or later |

FFmpeg is consumed only through its documentation and `libav*` headers — none
of FFmpeg's GPL-only files are used — so the governing FFmpeg license is the
LGPL v2.1. x264 and x265 are both GPL v2-or-later (each is also separately
available under a commercial license from its vendor).

## How the obligations are met

These artifacts are **generated at build time and are not committed** to this
repository (they are gitignored), so the repository tree itself remains 100%
MIT. The deploy workflow runs the extractor, which:

1. Fetches each upstream's verbatim license text into
   `web/public/licenses/` — `LICENSE_FFMPEG.txt` (LGPL v2.1), `LICENSE_X264.txt`
   and `LICENSE_X265.txt` (GPL v2). Nothing copyleft is vendored in the repo.
2. Emits an aggregate `web/public/THIRD-PARTY-NOTICES.html` listing each
   upstream, its license, copyright, the exact snapshots used, and a link to
   the corresponding source.
3. Stamps every rendered reference page (`ffmpeg-all.html`,
   `x264-reference.html`, `x265-reference.html`) with a footer stating it is
   generated documentation — not the original source — and linking the bundled
   license text and the corresponding upstream source at the exact tag/commit.

The deployed SPA links to the notices page from its footer.

## Upstream sources

- FFmpeg — <https://github.com/FFmpeg/FFmpeg>
- x264 — <https://code.videolan.org/videolan/x264>
- x265 — <https://bitbucket.org/multicoreware/x265_git>
