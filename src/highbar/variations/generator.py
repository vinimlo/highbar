"""Semantic variation generator — LLM-powered paraphrasing.

From one trigger, produces N realistic ways a user might say the same thing. That is
what `variations: N` in a spec buys: the agent has to route the intent correctly no
matter how the sentence is worded, not just for the one phrasing the product team
happened to write.

The generated set is cached on disk (see `cache.py`) and meant to be committed — so a
run is deterministic, offline, and free after the first generation.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from highbar.variations.cache import VariationCache, fingerprint

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Paraphrasing is an easy task; the cheapest capable model is the right default because
# this runs N times per test.
DEFAULT_MODEL = "gpt-4o-mini"

# Some spread, or the model returns N rewordings of the same sentence.
TEMPERATURE = 0.9

PROMPT = """\
You are helping test a conversational AI agent.

Rewrite the message below in {count} different ways that a REAL user might type it. \
Keep the intent identical — same request, same entities, same values — but vary the \
wording, tone, formality and length. Include the sloppiness of real typing: lowercase, \
missing punctuation, abbreviations, typos.

Do NOT answer the message. Do NOT add new information. Preserve any names, numbers, \
IDs and quantities exactly as written.

Message: {trigger}

Reply with a JSON array of {count} strings, and nothing else."""


class VariationError(RuntimeError):
    """Raised when variations are required but cannot be produced."""


class VariationGenerator:
    """Produces paraphrases of a trigger, from cache when possible.

    A cache hit never touches the network. A miss requires an API key — and when the
    key is absent, this raises instead of quietly running the test with the original
    trigger only: silently testing one phrasing while the spec asks for ten is a false
    sense of coverage, which is worse than an error.

    With ``frozen=True`` a miss always raises, key or no key. That is what CI wants: a
    spec whose variations were never committed should fail the build, not be generated
    on the spot and tested against sentences nobody reviewed.
    """

    def __init__(
        self,
        cache: VariationCache,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        frozen: bool = False,
    ) -> None:
        self._cache = cache
        self._frozen = frozen
        self._api_key = api_key if api_key is not None else os.environ.get("HIGHBAR_LLM_API_KEY")
        self._base_url = (
            base_url or os.environ.get("HIGHBAR_LLM_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._model = model or os.environ.get("HIGHBAR_LLM_MODEL") or DEFAULT_MODEL

    @property
    def model(self) -> str:
        return self._model

    def generate(self, trigger: str, count: int) -> list[str]:
        """Return `count` paraphrases of `trigger`, generating them only on a cache miss."""
        key = fingerprint(trigger, count)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if self._frozen:
            msg = (
                f"No cached variations for {trigger!r} and the run is frozen.\n"
                f"Generate them with `highbar variations <spec>` and commit {self._cache.path}."
            )
            raise VariationError(msg)

        variations = self._call_llm(trigger, count)
        self._cache.put(key, trigger=trigger, count=count, model=self._model, variations=variations)
        self._cache.save()
        return variations

    def _call_llm(self, trigger: str, count: int) -> list[str]:
        if not self._api_key:
            # Pointing at `highbar variations` alone would send the user to a command
            # that fails the same way: say what it needs, and the way around it.
            msg = (
                f"No cached variations for {trigger!r}. Set HIGHBAR_LLM_API_KEY to generate "
                f"them with `highbar variations`, or write them by hand in {self._cache.path}."
            )
            raise VariationError(msg)

        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "temperature": TEMPERATURE,
                    "messages": [
                        {"role": "user", "content": PROMPT.format(trigger=trigger, count=count)}
                    ],
                },
                timeout=60.0,
            )
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            msg = (
                f"Could not generate variations for {trigger!r}: "
                f"request to {self._base_url} failed: {exc}"
            )
            raise VariationError(msg) from exc

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            msg = f"Unexpected reply from {self._base_url}: not a chat completion"
            raise VariationError(msg) from exc

        if not isinstance(content, str):
            msg = f"Unexpected reply from {self._base_url}: message has no text content"
            raise VariationError(msg)
        return _parse_variations(content, count, trigger=trigger)


def _normalize(sentence: str) -> str:
    return " ".join(sentence.casefold().split())


def _parse_variations(content: str, count: int, trigger: str = "") -> list[str]:
    """Pull the list of strings out of the model's reply.

    Models wrap JSON in prose or fences even when told not to, so the array is extracted
    rather than assumed to be the whole message.

    Repeats — of each other or of ``trigger`` itself — are dropped: running the same
    sentence three times is one phrasing tested, not three.
    """
    text = content.strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        msg = f"Could not find a JSON array in the model reply: {content[:200]!r}"
        raise VariationError(msg)

    try:
        parsed: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        msg = f"Model reply is not valid JSON: {content[:200]!r}"
        raise VariationError(msg) from exc

    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        msg = f"Expected a JSON array of strings, got: {content[:200]!r}"
        raise VariationError(msg)

    seen = {_normalize(trigger)}
    variations: list[str] = []
    for item in parsed:
        sentence = item.strip()
        if not sentence or _normalize(sentence) in seen:
            continue
        seen.add(_normalize(sentence))
        variations.append(sentence)

    if not variations:
        msg = "Model returned no usable variations"
        raise VariationError(msg)

    # Asking for N and getting N-1 is common and harmless — testing 9 phrasings instead
    # of 10 is still testing robustness — as long as the report says 9: callers show
    # how many were actually produced, never the number requested. Getting MORE than
    # asked would silently inflate the run, so that is trimmed.
    return variations[:count]
