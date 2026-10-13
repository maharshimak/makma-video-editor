"""Model pricing.

Prices change more often than code ships, and a hardcoded table inside a
provider is wrong the week after it is written. This holds the table in one
place, lets a deployment replace it without a code change, and -- the part
that matters -- distinguishes "this call cost nothing" from "nobody told me
what this model costs".

A model with no price reports `cost_known=False` rather than $0.00. A usage
page that quietly totals unknown costs as zero is worse than one that says
how many calls it could not price.

Override with `MAKMA_MODEL_PRICING`, either a path to a JSON file or inline
JSON:

    {"gpt-4o-mini": {"in": 0.15, "out": 0.6},
     "claude-opus-4-5": {"in": 5.0, "out": 25.0, "cache_read": 0.5}}

Figures are USD per million tokens. Keys are matched as prefixes, longest
first, so "claude-sonnet-4-5" covers "claude-sonnet-4-5-20250929".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

log = logging.getLogger("makma.ai.pricing")


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""
    tokens_in: float
    tokens_out: float
    cache_read: float | None = None


#: Published list prices as of 2026-09. Correct on the day it was written and
#: not automatically thereafter -- override it rather than trusting it.
DEFAULT_PRICING: dict[str, Price] = {
    "claude-opus-4-5": Price(5.0, 25.0, 0.5),
    "claude-sonnet-4-5": Price(3.0, 15.0, 0.3),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.1),
    "gpt-4o-mini": Price(0.15, 0.6),
    "gpt-4o": Price(2.5, 10.0),
}

_table: dict[str, Price] | None = None


def _parse(raw: str) -> dict[str, Price]:
    """Read an override, from a file path or an inline JSON object."""
    text = raw.strip()
    if not text.startswith("{"):
        path = Path(text)
        if not path.exists():
            log.warning("MAKMA_MODEL_PRICING points at %s, which does not "
                        "exist; using built-in prices", path)
            return {}
        text = path.read_text(encoding="utf-8")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("MAKMA_MODEL_PRICING is not valid JSON (%s); using "
                    "built-in prices", exc)
        return {}

    table: dict[str, Price] = {}
    for model, entry in (parsed or {}).items():
        if not isinstance(entry, dict):
            log.warning("pricing for %s is not an object; skipped", model)
            continue
        try:
            table[str(model)] = Price(
                tokens_in=float(entry["in"]),
                tokens_out=float(entry["out"]),
                cache_read=(float(entry["cache_read"])
                            if entry.get("cache_read") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("pricing for %s is unusable (%s); skipped", model, exc)
    return table


def table() -> dict[str, Price]:
    """The effective price list: defaults, with any override merged over."""
    global _table
    if _table is None:
        merged = dict(DEFAULT_PRICING)
        raw = (settings.model_pricing or "").strip()
        if raw:
            merged.update(_parse(raw))
        _table = merged
    return _table


def reset() -> None:
    """Drop the cached table. Used by tests and by settings changes."""
    global _table
    _table = None


def price_for(model: str) -> Price | None:
    """Longest matching prefix, so a dated model id inherits its family price."""
    if not model:
        return None
    prices = table()
    for prefix in sorted(prices, key=len, reverse=True):
        if model.startswith(prefix):
            return prices[prefix]
    return None


def estimate(model: str, tokens_in: int, tokens_out: int,
             cached_in: int = 0) -> tuple[float, bool]:
    """Cost in USD, and whether it is a real figure.

    Returns (0.0, False) for a model with no price. The caller must not treat
    that as free.
    """
    price = price_for(model)
    if price is None:
        return 0.0, False

    billable_in = max(0, tokens_in - cached_in)
    cost = (billable_in * price.tokens_in + tokens_out * price.tokens_out)
    if cached_in:
        # Cache reads are cheaper but not free; without a published rate,
        # charge them at the full input price rather than at nothing.
        rate = price.cache_read if price.cache_read is not None else price.tokens_in
        cost += cached_in * rate
    return cost / 1_000_000, True
