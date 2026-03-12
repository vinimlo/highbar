"""Tests for shared subset_match utility."""

from __future__ import annotations

from gauntlet.assertions._match import subset_match


class TestSubsetMatch:
    def test_none_expected_returns_true(self) -> None:
        assert subset_match(None, {"a": 1}) is True

    def test_non_dict_actual_returns_false(self) -> None:
        assert subset_match({"a": 1}, "not a dict") is False
        assert subset_match({"a": 1}, 42) is False
        assert subset_match({"a": 1}, [1, 2]) is False

    def test_subset_match_succeeds(self) -> None:
        assert subset_match({"a": 1}, {"a": 1, "b": 2}) is True

    def test_exact_match_succeeds(self) -> None:
        assert subset_match({"a": 1, "b": 2}, {"a": 1, "b": 2}) is True

    def test_missing_key_fails(self) -> None:
        assert subset_match({"a": 1, "c": 3}, {"a": 1, "b": 2}) is False

    def test_value_mismatch_fails(self) -> None:
        assert subset_match({"a": 1}, {"a": 2}) is False

    def test_empty_expected_matches_anything(self) -> None:
        assert subset_match({}, {"a": 1}) is True
        assert subset_match({}, {}) is True

    def test_none_actual_returns_false(self) -> None:
        assert subset_match({"a": 1}, None) is False
