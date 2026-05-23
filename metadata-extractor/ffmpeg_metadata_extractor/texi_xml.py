"""Run GNU ``makeinfo`` and load its ``--xml`` output as an ElementTree.

The extractor stages a tag's ``doc/`` directory in a temporary location so
``makeinfo`` can resolve ``@include`` references, then this module runs the
tool and returns a parsed XML root.

Texinfo's ``--xml`` output references a public DTD and uses entity names
(``&textrsquo;``, ``&bullet;`` …) that Python's stdlib XML parser cannot
resolve out of the box. The output is post-processed: the DOCTYPE is
stripped and each non-standard entity reference is replaced with its
Unicode equivalent before parsing.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = ["MakeinfoError", "resolve_makeinfo", "run_makeinfo", "run_makeinfo_html"]


class MakeinfoError(RuntimeError):
    """Raised when ``makeinfo`` cannot be found or fails on a document."""


# Entities defined in texinfo.dtd that the stdlib XML parser doesn't know
# about. We map each to a Unicode equivalent (or empty string for layout-only
# entities). Anything else is replaced with an empty string and logged.
_ENTITIES: dict[str, str] = {
    "textldquo": "“",
    "textrdquo": "”",
    "textlsquo": "‘",
    "textrsquo": "’",
    "textmdash": "—",
    "textndash": "–",
    "textellipsis": "…",
    "textohm": "Ω",
    "textdegree": "°",
    "bullet": "•",
    "minus": "−",
    "lbrace": "{",
    "rbrace": "}",
    "arobase": "@",
    "linebreak": "\n",
    "tie": " ",
    "registeredsymbol": "®",
    "copyrightsymbol": "©",
    "result": "⇒",
    "expansion": "→",
    "equiv": "≡",
    "error": "⊥",
    "point": "∗",
    "print": "⊣",
    "today": "today",
}

# Predefined XML entities — leave these alone.
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9_]*);")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)


def resolve_makeinfo() -> list[str]:
    """Return the command (as argv list) that invokes ``makeinfo``.

    Resolution order:

    1. ``FFMPEG_MAKEINFO`` environment variable (shell-split).
    2. ``makeinfo`` on PATH.
    3. On Windows: a Perl + ``makeinfo`` script pair under ``C:\\MSYS64``
       or ``C:\\msys64`` (MSYS2 default install locations).
    """
    override = os.environ.get("FFMPEG_MAKEINFO")
    if override:
        return shlex.split(override)

    found = shutil.which("makeinfo") or shutil.which("texi2any")
    if found:
        return [found]

    if os.name == "nt":
        for root in (r"C:\MSYS64", r"C:\msys64", r"C:\msys2"):
            script = Path(root) / "usr" / "bin" / "makeinfo"
            perl = Path(root) / "usr" / "bin" / "perl.exe"
            if script.exists() and perl.exists():
                return [str(perl), str(script)]

    raise MakeinfoError(
        "makeinfo (GNU texinfo) not found. Install the 'texinfo' package "
        "(MSYS2: pacman -S texinfo; Debian/Ubuntu: apt install texinfo; "
        "macOS: brew install texinfo) or set FFMPEG_MAKEINFO to its path."
    )


def _sanitize_output(text: str) -> str:
    """Strip DOCTYPE and replace non-standard entity refs with their chars."""
    text = _DOCTYPE_RE.sub("", text, count=1)

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_ENTITIES:
            return match.group(0)
        replacement = _ENTITIES.get(name)
        if replacement is None:
            return ""
        return replacement

    return _ENTITY_RE.sub(_sub, text)


def run_makeinfo(
    input_path: Path,
    *,
    cwd: Path | None = None,
    cmd: list[str] | None = None,
) -> ET.Element:
    """Run ``makeinfo --xml`` on ``input_path`` and return the parsed root.

    ``cmd`` overrides the auto-resolved makeinfo invocation; pass the result
    of :func:`resolve_makeinfo` once per extractor run to avoid re-probing.
    """
    invocation = list(cmd) if cmd is not None else resolve_makeinfo()
    args = [
        *invocation,
        "--xml",
        "--no-validate",
        "--no-headers",
        "--force",
        "--error-limit=0",
        "-o", "-",
        str(input_path),
    ]

    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise MakeinfoError(f"Could not execute makeinfo: {exc}") from exc

    # makeinfo returns non-zero for unresolved nodes and other recoverable
    # issues even with --no-validate --force. We accept any output that
    # parses; only treat missing stdout as a hard failure.
    if not result.stdout.strip():
        raise MakeinfoError(
            f"makeinfo produced no XML output for {input_path.name} "
            f"(exit {result.returncode}): {result.stderr.strip()[:500]}"
        )

    cleaned = _sanitize_output(result.stdout)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise MakeinfoError(
            f"Could not parse makeinfo XML for {input_path.name}: {exc}"
        ) from exc


def run_makeinfo_html(
    input_path: Path,
    output_path: Path,
    *,
    cwd: Path | None = None,
    cmd: list[str] | None = None,
    init_file: Path | None = None,
) -> None:
    """Run ``makeinfo --html --no-split`` and write the rendered page to ``output_path``.

    Mirrors the upstream FFmpeg Makefile invocation
    (``makeinfo --html -I doc --no-split -D config-all --init-file=doc/t2h.pm
    --output <out> <in>``). ``-D config-all`` is omitted because the synthetic
    ``config.texi`` already force-sets it.

    The output path is resolved to absolute because ``cwd`` is typically the
    staged ``doc/`` directory and a relative output would resolve against that,
    silently writing to the wrong place (makeinfo does not create parent dirs
    and reports no error when the write target is unreachable).
    """
    invocation = list(cmd) if cmd is not None else resolve_makeinfo()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_output = output_path.resolve()

    args = [
        *invocation,
        "--html",
        "--no-split",
        "--no-validate",
        "--force",
        "--error-limit=0",
        "-I", ".",
    ]
    if init_file is not None:
        args.append(f"--init-file={init_file}")
    args.extend(["--output", str(absolute_output), str(input_path)])

    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise MakeinfoError(f"Could not execute makeinfo: {exc}") from exc

    if not absolute_output.exists() or absolute_output.stat().st_size == 0:
        stderr = result.stderr.strip()
        raise MakeinfoError(
            f"makeinfo produced no HTML output for {input_path.name} "
            f"(exit {result.returncode}): "
            f"{stderr[-2000:] if len(stderr) > 2000 else stderr}"
        )
