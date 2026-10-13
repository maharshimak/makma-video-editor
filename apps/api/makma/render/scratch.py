"""Filter-safe paths for files referenced from inside a filtergraph.

Most media reaches FFmpeg through `-i`, which is a plain argv entry and needs
no escaping. A handful of options -- `drawtext`'s fontfile/textfile, `lut3d`'s
file -- are read from inside the filtergraph, which FFmpeg unescapes twice.
A literal apostrophe cannot survive that round trip, so rather than emit a
graph that breaks on paths like `C:/Mak'ma Editz/...`, we stage those files
under a directory whose path is known to be safe.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from ..config import settings

#: Characters that cannot be represented inside a quoted filtergraph value.
UNSAFE_CHARS = ("'", "\n", "\r")


def is_filter_safe(path: str | Path) -> bool:
    text = str(path)
    return not any(ch in text for ch in UNSAFE_CHARS)


def _candidate_roots() -> list[Path]:
    roots = [Path(settings.tmp_root) / "fx", Path(tempfile.gettempdir()) / "makma-fx"]
    drive = Path(Path.cwd().anchor or "/")
    roots.append(drive / "makma-fx")
    return roots


def scratch_dir() -> Path:
    """The first writable scratch directory whose path is filter-safe."""
    for root in _candidate_roots():
        if not is_filter_safe(root):
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return root
        except OSError:
            continue
    raise RuntimeError(
        "No writable scratch directory with a filtergraph-safe path was found. "
        "Set MAKMA_DATA_DIR to a path without quote characters."
    )


def write_text_asset(text: str, suffix: str = ".txt") -> Path:
    """Write text to a filter-safe file, named by content so it dedupes."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    path = scratch_dir() / f"text_{digest}{suffix}"
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return path


def stage_readable(source: str | Path) -> Path:
    """Return a filter-safe path for an existing file, copying it if needed."""
    src = Path(source)
    if is_filter_safe(src):
        return src
    digest = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:16]
    dest = scratch_dir() / f"{digest}{src.suffix}"
    if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
        shutil.copy2(src, dest)
    return dest
