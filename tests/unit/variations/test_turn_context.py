"""HB-16: a conversation's last turn is paraphrased knowing what came before it.

"and the deadline?" means nothing alone. The generator gets the earlier user messages,
and the cache keys on them, so the same follow-up in two conversations is two entries.
A single-turn trigger keeps the key it always had: committed caches stay valid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from highbar.variations.cache import VariationCache, fingerprint
from highbar.variations.generator import VariationGenerator

EARLIER = ("I want to cancel my training",)


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._content}}]}


def test_hb16_a_single_turn_trigger_keeps_its_key() -> None:
    assert fingerprint("and the deadline?", 3, ()) == fingerprint("and the deadline?", 3)


def test_hb16_the_same_follow_up_after_another_conversation_is_another_entry() -> None:
    alone = fingerprint("and the deadline?", 3)
    after = fingerprint("and the deadline?", 3, EARLIER)

    assert after != alone
    assert fingerprint("and the deadline?", 3, ("I want to change my plan",)) != after


def test_hb16_the_prompt_has_the_conversation_before_the_message(tmp_path: Path) -> None:
    generator = VariationGenerator(
        VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m"
    )

    with patch("httpx.post", return_value=_Response('["what about the deadline?"]')) as post:
        got = generator.generate("and the deadline?", 1, context=EARLIER)

    assert got == ["what about the deadline?"]
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "I want to cancel my training" in prompt
    assert "and the deadline?" in prompt
    saved = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    (entry,) = saved["entries"].values() if "entries" in saved else saved.values()
    assert entry["context"] == list(EARLIER)
