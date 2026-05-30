"""Per-tag logger plus the cross-process queue/drain machinery that lets
parallel workers stream log lines through the parent (the sole console
writer) without tearing on Windows.

A cross-process lock alone is not enough: it serializes the Python-level
``write()`` call, but on Windows several processes writing to the same
console handle still render torn — the visible symptom was raw ``\\r\\n``
bytes mid-line shown as ``♪◙`` (CP437 for CR/LF) under ``--jobs > 1``.
Funnelling every line back to the single parent writer eliminates that:
only one process ever touches the console, exactly as in sequential mode.
"""

from __future__ import annotations

import sys
import threading

# Log fan-in queue for parallel workers. ``None`` in the parent and in
# sequential runs (where the single process writes to the console directly);
# set in pool workers via :func:`set_log_queue`. When set, a worker ships
# each log line to the parent instead of writing it itself, and the parent's
# drain thread is the *sole* writer to the console.
_log_queue = None

# Serializes the parent's own console writes (failure/summary lines emitted
# from the main thread) against the queue-drain thread — both run in the
# parent process during parallel extraction. Unused in workers.
_console_lock = threading.Lock()


def set_log_queue(queue) -> None:
    """Install the parent-side log queue in this worker process. Called by
    the pool initializer; in sequential mode the queue stays ``None`` and
    every emit goes straight to the console."""
    global _log_queue
    _log_queue = queue


def _emit_to_console(stream: str, line: str) -> None:
    """Write one line to the real console as a single ``write()+flush()``.

    Only ever called in the parent (or in a sequential run) — the
    ``_console_lock`` serializes the drain thread against the main thread.
    """
    target = sys.stderr if stream == "stderr" else sys.stdout
    with _console_lock:
        target.write(line + "\n")
        target.flush()


def _write_line(stream: str, line: str) -> None:
    """Emit a log line. In a pool worker (``_log_queue`` set) the line is
    shipped to the parent's drain thread; otherwise it goes straight to the
    console. This keeps a single process as the sole console writer."""
    if _log_queue is not None:
        _log_queue.put((stream, line))
    else:
        _emit_to_console(stream, line)


def drain_log_queue(log_queue) -> None:
    """Parent-side worker: write queued worker log lines to the console until
    the sentinel (``None``) arrives. The single console writer for pooled mode."""
    while True:
        item = log_queue.get()
        if item is None:
            return
        stream, line = item
        _emit_to_console(stream, line)


class Logger:
    """Per-tag log emitter.

    ``tag`` (when set) is prepended to every line as ``[{tag}] `` so output
    from concurrently-extracting workers stays attributable. Writes happen
    immediately — no buffering — so users see progress as it streams. In
    pooled mode each line is shipped to the parent's drain thread (the sole
    console writer) via ``_log_queue``.
    """

    def __init__(self, verbose: bool, *, tag: str | None = None) -> None:
        self._verbose = verbose
        self._tag = tag

    def _emit(self, stream: str, message: str) -> None:
        line = f"[{self._tag}] {message}" if self._tag else message
        _write_line(stream, line)

    def info(self, message: str) -> None:
        self._emit("stdout", message)

    def debug(self, message: str) -> None:
        if self._verbose:
            self._emit("stdout", message)

    def warn(self, message: str) -> None:
        self._emit("stderr", f"WARNING: {message}")
