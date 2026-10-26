"""On-disk cache of generated variations.

## Why a cache is not an optimization here

It is a correctness requirement. If paraphrases were generated fresh on every run, the
suite would test a different set of sentences each time — CI would go red on a commit
that touched nothing, and green again on a re-run. A behaviour gate that flickers is
not a gate.

So variations are generated **once**, written here, and committed. From then on the
suite is deterministic and runs offline: no API key, no network, no spend.

## What the key includes, and what it deliberately does not

The key is ``(trigger, count)`` — change either and the entry misses, which is exactly
when regenerating is correct.

The **model is not part of the key**, on purpose. If it were, CI would only hit the
cache when its ``HIGHBAR_LLM_MODEL`` happened to match whatever the developer had set
months earlier when generating; otherwise it would miss, demand an API key, and fail.
The committed file is the source of truth — the model only matters at generation time,
and is recorded as metadata so you can see what wrote the text.

## The file is meant to be read

Entries carry the trigger and the model alongside the sentences, so the JSON diffs
legibly in review. That matters: a language model can produce a paraphrase that quietly
changes the intent ("cancel my order" → "can I change my order?"), and the only place
to catch that is a human reading the generated set.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from highbar._narrow import as_dict, as_list

DEFAULT_CACHE_PATH = ".highbar/variations.json"


class CacheError(RuntimeError):
    """Raised when the cache file exists but cannot be used."""


def find_cache_path(start: Path | None = None) -> str:
    """Locate the committed cache from wherever the command was run.

    Walks up from ``start`` (the cwd by default) to the first directory that already
    holds a cache file, so running from a subdirectory of the project finds the same
    file that was generated at its root. Falls back to a path relative to ``start``.
    """
    origin = (start or Path.cwd()).resolve()
    for directory in (origin, *origin.parents):
        candidate = directory / DEFAULT_CACHE_PATH
        if candidate.is_file():
            return str(candidate)
    return str(origin / DEFAULT_CACHE_PATH)


def fingerprint(trigger: str, count: int) -> str:
    """Stable key for a (trigger, count) pair."""
    payload = json.dumps({"trigger": trigger, "count": count}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class VariationCache:
    """Reads and writes generated variations as a JSON file.

    The file is read on first use, not on construction: a run whose specs ask for no
    variations never touches it, and so cannot be broken by it.
    """

    def __init__(self, path: str = DEFAULT_CACHE_PATH) -> None:
        self._path = Path(path)
        self._loaded: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _entries(self) -> dict[str, Any]:
        if self._loaded is None:
            self._loaded = self._load()
        return self._loaded

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}

        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}

        # The file is committed and edited by several branches, so a bad merge is the
        # likely way for it to break. Say so instead of surfacing a JSONDecodeError.
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = (
                f"{self._path} is not valid JSON (line {exc.lineno}: {exc.msg}). "
                "Leftover merge conflict markers? Fix the file, or delete it and "
                "regenerate with `highbar variations`."
            )
            raise CacheError(msg) from exc

        entries = as_dict(data)
        if entries is None:
            msg = f"{self._path} must contain a JSON object, got {type(data).__name__}"
            raise CacheError(msg)
        return entries

    def get(self, key: str) -> list[str] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None

        fields = as_dict(entry)
        variations = as_list(fields.get("variations")) if fields is not None else None
        if variations is None or not all(isinstance(v, str) for v in variations):
            msg = f"{self._path}: entry {key!r} has no valid 'variations' list"
            raise CacheError(msg)
        return variations

    def put(self, key: str, trigger: str, count: int, model: str, variations: list[str]) -> None:
        self._entries[key] = {
            "trigger": trigger,
            "count": count,
            "model": model,
            "variations": variations,
        }

    def invalidate(self, key: str) -> None:
        """Drop an entry so the next `generate` regenerates it."""
        self._entries.pop(key, None)

    def save(self) -> None:
        """Writes the cache. Sorted keys so the file diffs cleanly when committed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False: the file is reviewed by humans, and "card\u00e1pio" is not
        # something a reviewer can check a paraphrase against.
        text = json.dumps(self._entries, indent=2, sort_keys=True, ensure_ascii=False)
        self._path.write_text(text + "\n", encoding="utf-8")
