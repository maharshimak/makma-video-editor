"""Typed FFmpeg abstraction.

Raw command strings never appear outside this module. Everything else builds
an `FFmpegCommand`, which knows how to render itself to argv, run with live
progress, and raise a useful error containing the tail of stderr.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..config import settings

log = logging.getLogger("makma.ffmpeg")

ProgressCallback = Callable[[float, str], None]


class FFmpegError(RuntimeError):
    def __init__(self, message: str, command: list[str], stderr: str) -> None:
        self.command = command
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-25:])
        super().__init__(f"{message}\n\ncommand: {shlex.join(command)}\n\n{tail}")


@dataclass
class Input:
    path: str | Path
    #: Options placed *before* -i (seek, loop, framerate, duration).
    options: list[str] = field(default_factory=list)

    def argv(self) -> list[str]:
        return [*self.options, "-i", str(self.path)]


@dataclass
class Output:
    path: str | Path
    options: list[str] = field(default_factory=list)

    def argv(self) -> list[str]:
        return [*self.options, str(self.path)]


@dataclass
class FFmpegCommand:
    inputs: list[Input] = field(default_factory=list)
    filter_complex: str | None = None
    outputs: list[Output] = field(default_factory=list)
    global_options: list[str] = field(default_factory=list)
    #: Wall-clock duration of the expected output, used for progress.
    expected_duration: float = 0.0

    def add_input(self, path: str | Path, *options: str) -> int:
        self.inputs.append(Input(path, list(options)))
        return len(self.inputs) - 1

    def add_output(self, path: str | Path, *options: str) -> None:
        self.outputs.append(Output(path, list(options)))

    def argv(self) -> list[str]:
        argv = [settings.ffmpeg_path, "-hide_banner", "-y", "-nostdin"]
        argv += self.global_options
        for inp in self.inputs:
            argv += inp.argv()
        if self.filter_complex:
            argv += ["-filter_complex", self.filter_complex]
        for out in self.outputs:
            argv += out.argv()
        return argv

    def to_string(self) -> str:
        return shlex.join(self.argv())

    def run(
        self,
        on_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> str:
        return run_ffmpeg(
            self.argv(),
            expected_duration=self.expected_duration,
            on_progress=on_progress,
            timeout=timeout,
        )


_TIME_RE = re.compile(r"out_time_ms=(-?\d+)")
_PROGRESS_RE = re.compile(r"^progress=(\w+)", re.M)


def run_ffmpeg(
    argv: Sequence[str],
    expected_duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    timeout: float | None = None,
) -> str:
    """Run FFmpeg, streaming progress. Returns stderr text on success."""
    cmd = list(argv)
    if on_progress is not None and "-progress" not in cmd:
        # Insert machine-readable progress right after the binary.
        cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]

    log.debug("ffmpeg: %s", shlex.join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > 4000:
                del stderr_chunks[:2000]

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    progress_errors: list[Exception] = []

    def _drain_stdout() -> None:
        try:
            if on_progress is not None and proc.stdout is not None:
                for line in proc.stdout:
                    m = _TIME_RE.search(line)
                    if m and expected_duration > 0:
                        seconds = max(0.0, int(m.group(1)) / 1_000_000)
                        on_progress(min(0.999, seconds / expected_duration), "encoding")
                    elif line.startswith("progress=end"):
                        on_progress(1.0, "encoding")
            elif proc.stdout is not None:
                proc.stdout.read()
        except Exception as exc:
            progress_errors.append(exc)

    out_thread = threading.Thread(target=_drain_stdout, daemon=True)
    out_thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        raise FFmpegError("FFmpeg timed out", cmd, "".join(stderr_chunks))

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    if progress_errors:
        raise progress_errors[0]
    stderr = "".join(stderr_chunks)
    if proc.returncode != 0:
        raise FFmpegError(f"FFmpeg exited {proc.returncode}", cmd, stderr)
    return stderr


def run_ffprobe(args: Sequence[str], timeout: float = 120) -> dict:
    """Run ffprobe with JSON output and parse the result."""
    cmd = [settings.ffprobe_path, "-v", "error", "-print_format", "json", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe exited {proc.returncode}", cmd, proc.stderr)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned invalid JSON: {exc}", cmd, proc.stdout)


def ffmpeg_available() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True, text=True, timeout=20,
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return proc.returncode == 0, first
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, str(exc)


# ---------------------------------------------------------------------------
# Filter-graph helpers
# ---------------------------------------------------------------------------

def escape_filter_value(value: str) -> str:
    """Escape a string for use inside a filtergraph option value."""
    out = value.replace("\\", "\\\\")
    for ch in (":", "'", "[", "]", ",", ";"):
        out = out.replace(ch, "\\" + ch)
    return out


def escape_drawtext(value: str) -> str:
    """drawtext needs its own escaping for text and file paths."""
    out = value.replace("\\", "\\\\\\\\")
    out = out.replace(":", "\\:").replace("'", "’")
    out = out.replace("%", "\\%")
    return out


def quote_filter_arg(value: str | Path) -> str:
    """Quote a value for use as a filter option, including the quotes.

    FFmpeg parses a filtergraph in two passes. Quoting protects a value from
    the first pass (which splits on `,` `;` `[` `]`) but *not* from the second,
    which still splits options on `:` -- so a Windows drive letter has to be
    escaped even inside quotes. A literal apostrophe cannot survive the round
    trip at all; callers stage such files through `render.scratch` first.
    """
    text = str(value).replace("\\", "/").replace(":", "\\:")
    return "'" + text + "'"


def escape_path_for_filter(path: str | Path) -> str:
    """Unquoted form: escape every character the two parsers treat specially."""
    p = str(path).replace("\\", "/")
    for ch in (":", "'", "[", "]", ",", ";"):
        p = p.replace(ch, "\\" + ch)
    return p


class FilterChain:
    """Accumulates a linear chain of filters between two labels."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.filters: list[str] = []

    def add(self, expr: str | None) -> "FilterChain":
        if expr:
            self.filters.append(expr)
        return self

    def build(self, sink: str) -> str | None:
        if not self.filters:
            return None
        return f"[{self.source}]{','.join(self.filters)}[{sink}]"

    @property
    def empty(self) -> bool:
        return not self.filters
