"""Rewrite src/highbar/engine/prices.json from OpenRouter's public list of models.

    uv run python scripts/update_prices.py                 # from the live list
    uv run python scripts/update_prices.py models.json     # from a saved copy of it

Review the diff before committing it: a new price changes the cost of a run that did not
change, and a report says nothing about which table priced it.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from highbar.engine.usage import read_prices

SOURCE = "https://openrouter.ai/api/v1/models"
PRICES = Path(__file__).resolve().parents[1] / "src" / "highbar" / "engine" / "prices.json"
KEPT = ("prompt", "completion", "input_cache_read")


def prices_from(models: dict[str, Any]) -> dict[str, Any]:
    """The table: each model's id and the prices highbar uses, sorted to keep diffs small.

    A model highbar could not price stays out: a router's price depends on the model it
    picks, and OpenRouter lists it as negative.
    """
    entries = [
        {
            "id": model["id"],
            "pricing": {key: model["pricing"][key] for key in KEPT if key in model["pricing"]},
        }
        for model in models["data"]
    ]
    return {
        "currency": "USD",
        "source": f"{SOURCE}: USD per token, written by scripts/update_prices.py.",
        "data": sorted(
            (entry for entry in entries if read_prices({"data": [entry]})),
            key=lambda entry: entry["id"],
        ),
    }


def main(arguments: list[str]) -> None:
    if arguments:
        text = Path(arguments[0]).read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(SOURCE, timeout=30) as response:  # noqa: S310 — a fixed URL
            text = response.read().decode("utf-8")
    table = prices_from(json.loads(text))
    PRICES.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"{len(table['data'])} models written to {PRICES}")


if __name__ == "__main__":
    main(sys.argv[1:])
