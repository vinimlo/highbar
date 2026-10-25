"""Tests for shared subset_match utility."""

from __future__ import annotations

import pytest

from highbar.assertions._match import subset_match, validate_expected


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


class TestMatchers:
    """Matcher dicts in expected values: contains, icontains, matches, equals and negations."""

    def test_icontains_ignores_case(self) -> None:
        """The motivating bug: the model writes 'Coca-Cola' one run and 'coca-cola' the next."""
        assert subset_match({"product": {"icontains": "coca"}}, {"product": "Coca-Cola"})
        assert subset_match({"product": {"icontains": "COCA"}}, {"product": "coca-cola"})

    def test_contains_is_case_sensitive_like_response_contains(self) -> None:
        assert subset_match({"product": {"contains": "Coca"}}, {"product": "Coca-Cola"})
        assert not subset_match({"product": {"contains": "coca"}}, {"product": "Coca-Cola"})
        assert not subset_match({"country": {"contains": "US"}}, {"country": "business"})

    def test_contains_fails_when_absent(self) -> None:
        assert not subset_match({"product": {"contains": "pizza"}}, {"product": "Coca-Cola"})
        assert not subset_match({"product": {"icontains": "pizza"}}, {"product": "Coca-Cola"})

    def test_matches_applies_a_regex(self) -> None:
        assert subset_match({"order_id": {"matches": r"^\d+$"}}, {"order_id": "12345"})
        assert not subset_match({"order_id": {"matches": r"^\d+$"}}, {"order_id": "abc"})

    def test_text_matchers_coerce_numbers(self) -> None:
        """`contains` and `matches` agree on what a number looks like."""
        assert subset_match({"qty": {"matches": r"^\d+$"}}, {"qty": 42})
        assert subset_match({"order_id": {"contains": "123"}}, {"order_id": 12345})
        assert subset_match({"order_id": {"not_contains": "9"}}, {"order_id": 12345})

    @pytest.mark.parametrize("actual", [None, {"code": "abc"}, ["abc"]])
    def test_matches_never_matches_none_or_containers(self, actual: object) -> None:
        """str(None) is 'None', which would otherwise satisfy ^[a-zA-Z]+$."""
        assert not subset_match({"v": {"matches": r"^[a-zA-Z]+$"}}, {"v": actual})
        assert not subset_match({"v": {"matches": ".*"}}, {"v": actual})

    def test_not_contains(self) -> None:
        assert subset_match({"text": {"not_contains": "pizza"}}, {"text": "Coca-Cola"})
        assert not subset_match({"text": {"not_contains": "Coca"}}, {"text": "Coca-Cola"})
        assert subset_match({"text": {"not_contains": "coca"}}, {"text": "Coca-Cola"})
        assert not subset_match({"text": {"not_icontains": "coca"}}, {"text": "Coca-Cola"})

    @pytest.mark.parametrize("key", ["not_contains", "not_icontains"])
    @pytest.mark.parametrize("actual", [None, {"code": "timeout"}, {"code": 504}])
    def test_negation_fails_when_it_cannot_be_evaluated(self, key: str, actual: object) -> None:
        """A negative assertion on None or a dict checked nothing, so it must not be green."""
        assert not subset_match({"error": {key: "timeout"}}, {"error": actual})

    def test_contains_on_a_list_is_membership(self) -> None:
        assert subset_match({"tags": {"contains": "drink"}}, {"tags": ["drink", "cold"]})
        assert not subset_match({"tags": {"contains": "food"}}, {"tags": ["drink", "cold"]})
        assert not subset_match({"tags": {"contains": "Drink"}}, {"tags": ["drink", "cold"]})
        assert subset_match({"tags": {"icontains": "Drink"}}, {"tags": ["drink", "cold"]})
        assert subset_match({"tags": {"not_contains": "food"}}, {"tags": ["drink", "cold"]})
        assert not subset_match({"tags": {"not_contains": "drink"}}, {"tags": ["drink", "cold"]})

    def test_several_conditions_must_all_hold(self) -> None:
        matcher = {"name": {"icontains": "coca", "not_icontains": "zero"}}

        assert subset_match(matcher, {"name": "Coca-Cola"})
        assert not subset_match(matcher, {"name": "Coca-Cola Zero"})

    def test_matchers_work_at_any_depth(self) -> None:
        expected = {"order": {"status": {"contains": "ship"}}}

        assert subset_match(expected, {"order": {"status": "shipped", "id": 7}})
        assert not subset_match(expected, {"order": {"status": "pending", "id": 7}})

    def test_equals_escapes_a_literal_that_looks_like_a_matcher(self) -> None:
        """A param whose real value is `{"contains": "x"}` must still be comparable."""
        literal = {"contains": "x"}

        assert subset_match({"payload": {"equals": literal}}, {"payload": literal})
        assert not subset_match({"payload": {"equals": literal}}, {"payload": {"contains": "y"}})

    def test_equals_is_exact_for_nested_dicts(self) -> None:
        """The way to opt out of subset matching one level down."""
        assert subset_match({"opts": {"equals": {"size": "L"}}}, {"opts": {"size": "L"}})
        assert not subset_match(
            {"opts": {"equals": {"size": "L"}}}, {"opts": {"size": "L", "ice": True}}
        )


class TestLiteralsAreUnchanged:
    """The original behaviour must not regress: a plain value compares by equality."""

    def test_exact_equality_still_applies(self) -> None:
        assert subset_match({"order_id": "12345"}, {"order_id": "12345"})
        assert not subset_match({"order_id": "12345"}, {"order_id": "54321"})

    def test_case_still_matters_for_a_plain_string(self) -> None:
        assert not subset_match({"product": "coca-cola"}, {"product": "Coca-Cola"})

    def test_a_nested_dict_that_is_not_a_matcher_is_subset_matched(self) -> None:
        assert subset_match({"opts": {"size": "L"}}, {"opts": {"size": "L"}})
        assert subset_match({"opts": {"size": "L"}}, {"opts": {"size": "L", "ice": True}})
        assert not subset_match({"opts": {"size": "L"}}, {"opts": {"size": "M"}})
        assert not subset_match({"opts": {"size": "L"}}, {"opts": "L"})


class TestValidateExpected:
    """Malformed matchers are rejected up front, by the parser."""

    @pytest.mark.parametrize(
        "expected",
        [
            None,
            {"order_id": "123"},
            {"product": {"icontains": "coca", "not_icontains": "zero"}},
            {"order": {"status": {"matches": "^ship"}}},
            # The operand of `equals` is a literal: anything goes inside it.
            {"filter": {"equals": {"contain": "x", "matches": "[0-9"}}},
            {"opts": {"size": "L", "container": "cup"}},
        ],
    )
    def test_accepts_well_formed_values(self, expected: object) -> None:
        validate_expected(expected, "params")

    def test_rejects_an_invalid_regex(self) -> None:
        with pytest.raises(ValueError, match=r"params\.order_id: invalid regex"):
            validate_expected({"order_id": {"matches": "[0-9"}}, "params")

    def test_rejects_a_non_string_regex(self) -> None:
        with pytest.raises(ValueError, match="needs a regex string"):
            validate_expected({"order_id": {"matches": 123}}, "params")

    def test_rejects_a_misspelled_matcher(self) -> None:
        """`{contain: ...}` would otherwise be compared as a literal dict, silently."""
        with pytest.raises(ValueError, match="did you mean 'contains'"):
            validate_expected({"product": {"contain": "coca"}}, "params")

    def test_rejects_a_matcher_mixed_with_other_keys(self) -> None:
        with pytest.raises(ValueError, match="mixes matcher keys"):
            validate_expected({"product": {"contains": "coca", "size": "L"}}, "params")

    def test_rejects_a_bad_matcher_nested_in_a_literal_dict(self) -> None:
        with pytest.raises(ValueError, match=r"result\.order\.status"):
            validate_expected({"order": {"status": {"matches": "("}}}, "result")
