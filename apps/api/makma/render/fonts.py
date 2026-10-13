"""Font resolution for drawtext.

drawtext needs a real font file. We resolve a requested family to something
that exists on this machine and cache the answer, falling back to any usable
sans-serif rather than failing a render over typography.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

FONT_DIRS = [
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]

# family -> (regular, bold, italic, bold-italic) candidate filenames
FAMILIES: dict[str, tuple[list[str], list[str]]] = {
    "inter": (["Inter-Regular.ttf", "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
              ["Inter-Bold.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]),
    "arial": (["arial.ttf", "DejaVuSans.ttf"], ["arialbd.ttf", "DejaVuSans-Bold.ttf"]),
    "helvetica": (["arial.ttf", "Helvetica.ttc", "DejaVuSans.ttf"],
                  ["arialbd.ttf", "DejaVuSans-Bold.ttf"]),
    "impact": (["impact.ttf", "arialbd.ttf"], ["impact.ttf", "arialbd.ttf"]),
    "georgia": (["georgia.ttf", "times.ttf"], ["georgiab.ttf", "timesbd.ttf"]),
    "times": (["times.ttf", "DejaVuSerif.ttf"], ["timesbd.ttf", "DejaVuSerif-Bold.ttf"]),
    "courier": (["cour.ttf", "DejaVuSansMono.ttf"],
                ["courbd.ttf", "DejaVuSansMono-Bold.ttf"]),
    "bebas": (["BebasNeue-Regular.ttf", "impact.ttf", "arialbd.ttf"],
              ["BebasNeue-Regular.ttf", "impact.ttf", "arialbd.ttf"]),
    "oswald": (["Oswald-Regular.ttf", "segoeui.ttf", "arialbd.ttf"],
               ["Oswald-Bold.ttf", "segoeuib.ttf", "arialbd.ttf"]),
}

GENERIC_FALLBACKS = [
    "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf", "Helvetica.ttc", "verdana.ttf", "tahoma.ttf",
]


@lru_cache(maxsize=1)
def _index() -> dict[str, str]:
    """Map lowercase filename -> absolute path for every font we can see."""
    found: dict[str, str] = {}
    for directory in FONT_DIRS:
        if not directory.exists():
            continue
        try:
            for path in directory.rglob("*"):
                if path.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    found.setdefault(path.name.lower(), str(path))
        except (OSError, PermissionError):
            continue
    return found


@lru_cache(maxsize=128)
def resolve_font(family: str = "Inter", bold: bool = False) -> str | None:
    index = _index()
    key = (family or "").strip().lower()
    candidates: list[str] = []

    regular, bolded = FAMILIES.get(key, ([], []))
    candidates += bolded if bold else regular
    if not candidates and key:
        # Try the family name verbatim, e.g. "Montserrat" -> montserrat.ttf
        stem = key.replace(" ", "")
        candidates += [f"{stem}bd.ttf" if bold else f"{stem}.ttf",
                       f"{stem}-bold.ttf" if bold else f"{stem}-regular.ttf"]
    candidates += (["arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"] if bold else [])
    candidates += GENERIC_FALLBACKS

    for name in candidates:
        hit = index.get(name.lower())
        if hit:
            return hit
    return next(iter(index.values()), None)


def available_families() -> list[str]:
    """Families we can actually render with on this machine."""
    return sorted(
        name.title() for name in FAMILIES if resolve_font(name) is not None
    )
