"""Model pricing table with an explicit stamp and an env override.

Prices below were researched on 2026-10-24 (sources: provider pricing pages
aggregated by benchlm.ai, cloudzero.com, fast.io). Public sources disagreed on
some OpenAI figures, so:

- the ledger ALWAYS records true token counts; cost is derived, recomputable,
- an unknown model records ``cost_usd=None`` ("unknown"), never a guessed 0,
- ``SYNAPSE_PRICING_JSON`` overrides/extends the table without a code change,
  e.g. {"openai/gpt-5.2": {"input_per_mtok": 0.875, "output_per_mtok": 7.0}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

PRICING_STAMP = datetime(2026, 10, 24, tzinfo=UTC)


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float  # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens
    source: str = "bundled table (researched 2026-10-24)"


_PRICES: dict[str, ModelPrice] = {
    # Anthropic (researched 2026-10-24)
    "anthropic/claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "anthropic/claude-haiku-4-5": ModelPrice(1.0, 5.0),
    "anthropic/claude-opus-4-8": ModelPrice(5.0, 25.0),
    # OpenAI (researched 2026-10-24; sources conflicted — override via env
    # if your account bills differently)
    "openai/gpt-5.2": ModelPrice(1.75, 14.0),
    "openai/gpt-5-mini": ModelPrice(0.25, 2.0),
}


def _overrides() -> dict[str, ModelPrice]:
    raw = (os.environ.get("SYNAPSE_PRICING_JSON") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, ModelPrice] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                try:
                    out[str(key)] = ModelPrice(
                        input_per_mtok=float(value["input_per_mtok"]),
                        output_per_mtok=float(value["output_per_mtok"]),
                        source="SYNAPSE_PRICING_JSON override",
                    )
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def price_for(provider: str, model_id: str) -> ModelPrice | None:
    key = f"{provider}/{model_id}"
    table = {**_PRICES, **_overrides()}
    return table.get(key)


def cost_usd(price: ModelPrice | None, input_tokens: int, output_tokens: int) -> float | None:
    """Compute cost, or None when the model has no known price."""
    if price is None:
        return None
    return round(
        (input_tokens / 1_000_000) * price.input_per_mtok
        + (output_tokens / 1_000_000) * price.output_per_mtok,
        6,
    )
