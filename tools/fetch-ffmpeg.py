#!/usr/bin/env python3
"""Download a static FFmpeg build into tools/ffmpeg/bin.

The render engine needs both `ffmpeg` and `ffprobe`. If they are already on
PATH this script does nothing; otherwise it vendors a static build so a clean
checkout can render video without a system install.

    python tools/fetch-ffmpeg.py [--force]
"""
from __future__ import annotations

import argparse
import io
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "tools" / "ffmpeg" / "bin"

SOURCES = {
    "Windows": (
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "zip",
        ("ffmpeg.exe", "ffprobe.exe"),
    ),
    "Linux": (
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        "tar",
        ("ffmpeg", "ffprobe"),
    ),
}


def already_available() -> bool:
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary) and not (BIN_DIR / binary).exists() \
                and not (BIN_DIR / f"{binary}.exe").exists():
            return False
    return True


def fetch(url: str) -> bytes:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def extract(payload: bytes, kind: str, wanted: tuple[str, ...]) -> list[Path]:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if kind == "zip":
        archive = zipfile.ZipFile(io.BytesIO(payload))
        members = [n for n in archive.namelist()
                   if Path(n).name in wanted]
        for name in members:
            dest = BIN_DIR / Path(name).name
            with archive.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written.append(dest)
    else:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz")
        for member in archive.getmembers():
            if Path(member.name).name in wanted and member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                dest = BIN_DIR / Path(member.name).name
                dest.write_bytes(extracted.read())
                dest.chmod(0o755)
                written.append(dest)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="download even if FFmpeg is already available")
    args = parser.parse_args()

    if already_available() and not args.force:
        print("FFmpeg and ffprobe are already available; nothing to do.")
        return 0

    system = platform.system()
    if system not in SOURCES:
        print(
            f"No vendored build is configured for {system}. Install FFmpeg with "
            "your package manager (e.g. `brew install ffmpeg`) or set "
            "MAKMA_FFMPEG_PATH / MAKMA_FFPROBE_PATH.",
            file=sys.stderr,
        )
        return 1

    url, kind, wanted = SOURCES[system]
    written = extract(fetch(url), kind, wanted)
    if len(written) < 2:
        print(f"Expected {wanted} in the archive, found {written}", file=sys.stderr)
        return 1

    for path in written:
        print(f"  -> {path}")
    version = subprocess.run(
        [str(BIN_DIR / wanted[0]), "-version"], capture_output=True, text=True
    ).stdout.splitlines()[:1]
    print(version[0] if version else "installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
