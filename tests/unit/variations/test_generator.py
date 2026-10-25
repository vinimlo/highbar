"""Tests for the semantic variation generator and its cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from highbar.variations.cache import (
    CacheError,
    VariationCache,
    find_cache_path,
    fingerprint,
)
from highbar.variations.generator import VariationError, VariationGenerator, _parse_variations


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._content}}]}


class TestCache:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "variations.json"
        cache = VariationCache(str(path))
        cache.put("k", trigger="hi", count=2, model="m", variations=["a", "b"])
        cache.save()

        assert VariationCache(str(path)).get("k") == ["a", "b"]

    def test_entry_records_what_produced_it(self, tmp_path: Path) -> None:
        """The file gets committed and reviewed: a paraphrase can silently change intent."""
        path = tmp_path / "variations.json"
        cache = VariationCache(str(path))
        cache.put("k", trigger="hi", count=2, model="gpt-4o-mini", variations=["a", "b"])
        cache.save()

        written = json.loads(path.read_text())["k"]
        assert written["trigger"] == "hi"
        assert written["model"] == "gpt-4o-mini"

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert VariationCache(str(tmp_path / "nope.json")).get("k") is None

    def test_non_ascii_text_is_written_readable(self, tmp_path: Path) -> None:
        """The file is reviewed by humans; "card\\u00e1pio" cannot be reviewed."""
        path = tmp_path / "variations.json"
        cache = VariationCache(str(path))
        cache.put("k", trigger="cardápio", count=1, model="m", variations=["o que tá no cardápio?"])
        cache.save()

        written = path.read_text(encoding="utf-8")
        assert "o que tá no cardápio?" in written
        assert "\\u00e1" not in written
        assert VariationCache(str(path)).get("k") == ["o que tá no cardápio?"]

    def test_file_with_conflict_markers_gives_a_readable_error(self, tmp_path: Path) -> None:
        """A committed file edited by several branches breaks through bad merges."""
        path = tmp_path / "variations.json"
        path.write_text('{\n<<<<<<< HEAD\n  "k": {}\n=======\n  "j": {}\n>>>>>>> other\n}\n')

        with pytest.raises(CacheError, match="not valid JSON.*merge conflict"):
            VariationCache(str(path)).get("k")

    @pytest.mark.parametrize("content", ["[]", '{"k": {"trigger": "hi"}}', '{"k": ["a"]}'])
    def test_malformed_entries_give_a_readable_error(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "variations.json"
        path.write_text(content)

        with pytest.raises(CacheError):
            VariationCache(str(path)).get("k")

    def test_file_is_not_read_until_needed(self, tmp_path: Path) -> None:
        """A run with no variations must not be broken by a broken cache file."""
        path = tmp_path / "variations.json"
        path.write_text("not json")

        VariationCache(str(path))  # does not raise

    def test_cache_is_found_from_a_subdirectory(self, tmp_path: Path) -> None:
        cache_file = tmp_path / ".highbar" / "variations.json"
        cache_file.parent.mkdir()
        cache_file.write_text("{}")
        nested = tmp_path / "specs" / "orders"
        nested.mkdir(parents=True)

        assert find_cache_path(nested) == str(cache_file)

    def test_cache_defaults_to_the_starting_directory(self, tmp_path: Path) -> None:
        assert find_cache_path(tmp_path) == str(tmp_path / ".highbar" / "variations.json")

    def test_key_changes_with_trigger_and_count(self) -> None:
        """A cached entry must miss exactly when regenerating is the correct thing to do."""
        base = fingerprint("show me the menu", 5)

        assert fingerprint("show me the MENU", 5) != base
        assert fingerprint("show me the menu", 6) != base
        assert fingerprint("show me the menu", 5) == base


class TestGenerate:
    def test_cache_hit_works_even_with_a_different_model_configured(self, tmp_path: Path) -> None:
        """CI must hit the cache without knowing which model the developer generated with."""
        cache = VariationCache(str(tmp_path / "v.json"))
        cache.put(
            fingerprint("hi", 2),
            trigger="hi",
            count=2,
            model="gpt-4o-mini",
            variations=["hey", "yo"],
        )
        generator = VariationGenerator(cache, api_key="", model="some-other-model")

        assert generator.generate("hi", 2) == ["hey", "yo"]

    def test_cache_hit_never_calls_the_llm(self, tmp_path: Path) -> None:
        """The whole point: after generating once, runs are offline and free."""
        cache = VariationCache(str(tmp_path / "v.json"))
        generator = VariationGenerator(cache, api_key="sk-test", model="m")
        cache.put(fingerprint("hi", 2), trigger="hi", count=2, model="m", variations=["hey", "yo"])

        with patch("httpx.post") as post:
            assert generator.generate("hi", 2) == ["hey", "yo"]

        post.assert_not_called()

    def test_cache_miss_generates_and_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "v.json"
        generator = VariationGenerator(VariationCache(str(path)), api_key="sk-test", model="m")

        with patch("httpx.post", return_value=_Response('["hey", "yo"]')):
            assert generator.generate("hi", 2) == ["hey", "yo"]

        # Persisted, so the next process starts from the cache.
        assert VariationCache(str(path)).get(fingerprint("hi", 2)) == ["hey", "yo"]

    def test_missing_api_key_raises_instead_of_running_one_phrasing(self, tmp_path: Path) -> None:
        """Silently testing 1 phrasing when the spec asked for 10 is false coverage."""
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="", model="m"
        )

        with pytest.raises(VariationError, match="HIGHBAR_LLM_API_KEY"):
            generator.generate("hi", 10)

    def test_more_variations_than_asked_are_trimmed(self, tmp_path: Path) -> None:
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m"
        )

        with patch("httpx.post", return_value=_Response('["a", "b", "c", "d"]')):
            assert generator.generate("hi", 2) == ["a", "b"]

    def test_frozen_never_generates(self, tmp_path: Path) -> None:
        """CI: a spec whose variations were never committed fails, key or no key."""
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m", frozen=True
        )

        with patch("httpx.post") as post, pytest.raises(VariationError, match="frozen"):
            generator.generate("hi", 2)

        post.assert_not_called()

    def test_frozen_still_serves_the_cache(self, tmp_path: Path) -> None:
        cache = VariationCache(str(tmp_path / "v.json"))
        cache.put(fingerprint("hi", 2), trigger="hi", count=2, model="m", variations=["hey", "yo"])

        assert VariationGenerator(cache, api_key="", frozen=True).generate("hi", 2) == ["hey", "yo"]

    @pytest.mark.parametrize(
        "failure",
        [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.InvalidURL("bad url")],
    )
    def test_network_failures_become_variation_errors(
        self, tmp_path: Path, failure: Exception
    ) -> None:
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m"
        )

        with patch("httpx.post", side_effect=failure), pytest.raises(VariationError):
            generator.generate("hi", 2)

    def test_http_error_status_becomes_a_variation_error(self, tmp_path: Path) -> None:
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m"
        )
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(401, request=request)

        with patch("httpx.post", return_value=response), pytest.raises(VariationError):
            generator.generate("hi", 2)

    def test_reply_that_is_not_a_chat_completion_becomes_a_variation_error(
        self, tmp_path: Path
    ) -> None:
        generator = VariationGenerator(
            VariationCache(str(tmp_path / "v.json")), api_key="sk-test", model="m"
        )
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(200, request=request, json={"error": "nope"})

        with patch("httpx.post", return_value=response), pytest.raises(VariationError):
            generator.generate("hi", 2)

    def test_repeats_are_not_cached(self, tmp_path: Path) -> None:
        """Three copies of one sentence is one phrasing tested, not three."""
        path = tmp_path / "v.json"
        generator = VariationGenerator(VariationCache(str(path)), api_key="sk-test", model="m")
        reply = '["gimme the menu", "Gimme  the menu", "gimme the menu", "show me the menu"]'

        with patch("httpx.post", return_value=_Response(reply)):
            assert generator.generate("show me the menu", 5) == ["gimme the menu"]

        assert VariationCache(str(path)).get(fingerprint("show me the menu", 5)) == [
            "gimme the menu"
        ]


class TestParseReply:
    def test_extracts_the_array_from_surrounding_prose(self) -> None:
        """Models wrap JSON in fences and chatter even when told not to."""
        reply = 'Sure! Here you go:\n```json\n["one", "two"]\n```\nHope that helps.'

        assert _parse_variations(reply, 2) == ["one", "two"]

    def test_blank_entries_are_dropped(self) -> None:
        assert _parse_variations('["one", "  ", "two"]', 3) == ["one", "two"]

    @pytest.mark.parametrize(
        "reply",
        ["no array here", "[not, json]", '["ok", 3]', "[]"],
    )
    def test_unusable_reply_raises(self, reply: str) -> None:
        with pytest.raises(VariationError):
            _parse_variations(reply, 2)

    def test_duplicates_are_dropped(self) -> None:
        assert _parse_variations('["one", "ONE", " one ", "two"]', 5) == ["one", "two"]

    def test_the_trigger_itself_is_not_a_variation(self) -> None:
        assert _parse_variations('["Hi there", "hello"]', 5, trigger="hi there") == ["hello"]

    def test_only_repeats_of_the_trigger_is_unusable(self) -> None:
        with pytest.raises(VariationError):
            _parse_variations('["hi", "HI"]', 2, trigger="hi")
