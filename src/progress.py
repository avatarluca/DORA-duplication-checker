from __future__ import annotations
from typing import TextIO

import os
import shutil
import sys
import threading
import time


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _bar(done: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return "-" * width
    filled = min(width, max(0, int(width * done / total)))
    return "#" * filled + "-" * (width - filled)


def _fit(text: str, stream: TextIO) -> str:
    try:
        cols = shutil.get_terminal_size((120, 24)).columns
    except OSError:
        cols = 120
    width = max(40, cols - 1)
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


class Progress:
    def __init__(self, total: int, label: str = "", stream: TextIO | None = None) -> None:
        self.total = max(0, total)
        self.label = label
        self.started = time.time()
        self.done = 0
        self.stream = stream or sys.stderr
        self._width = 0
        self._lock = threading.RLock()

    def _line(self, extra: str = "") -> str:
        elapsed = time.time() - self.started
        total = self.total
        done = min(self.done, total) if total else self.done
        left = max(0, total - done)
        pct = (100.0 * done / total) if total else 100.0
        if done > 0 and left > 0 and elapsed > 0:
            eta = format_duration(elapsed * left / done)
            rate = f"  {done / elapsed * 60:.1f}/min"
        elif left == 0:
            eta = "0s"
            rate = ""
        else:
            eta = "?"
            rate = ""
        prefix = f"{self.label}  " if self.label else ""
        extra_s = f"  {extra}" if extra else ""
        return _fit(
            f"# {prefix}[{_bar(done, total)}] {done}/{total}  {pct:5.1f}%  "
            f"left {left}  eta {eta}{rate}  elapsed {format_duration(elapsed)}"
            f"{extra_s}",
            self.stream,
        )

    def update(self, done: int, extra: str = "", *, every: int = 1, newline: bool | None = None) -> None:
        with self._lock:
            self.done = done
            if done != self.total and every > 1 and done % every:
                return
            if newline is None:
                env_nl = (os.environ.get("PROGRESS_NEWLINE") or "").strip().lower()
                force_nl = env_nl in {"1", "true", "yes", "on"}
                newline = done >= self.total or not self.stream.isatty() or force_nl
            self._emit(newline=newline, extra=extra)

    def step(self, extra: str = "", *, every: int = 1, newline: bool | None = None) -> None:
        with self._lock:
            self.update(self.done + 1, extra, every=every, newline=newline)

    def finish(self, extra: str = "") -> None:
        with self._lock:
            if self.done >= self.total and self._width == 0 and not extra:
                return
            self.done = max(self.done, self.total)
            self._emit(newline=True, extra=extra)

    def _emit(self, *, newline: bool, extra: str) -> None:
        text = self._line(extra)
        if newline:
            pad = max(0, self._width - len(text))
            self.stream.write(("\r" if self._width else "") + text + (" " * pad) + "\n")
            self.stream.flush()
            self._width = 0
            return
        pad = max(0, self._width - len(text))
        self.stream.write("\r" + text + " " * pad)
        self.stream.flush()
        self._width = len(text)