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


class TestIgnoringCase:
    def test_imatches_finds_what_matches_misses(self) -> None:
        answer = {"note": "Onze anos de garantia"}

        assert subset_match({"note": {"imatches": "^onze anos"}}, answer)
        assert not subset_match({"note": {"matches": "^onze anos"}}, answer)
        assert not subset_match({"note": {"imatches": "^seis anos"}}, answer)

    def test_not_imatches_refuses_what_not_matches_lets_through(self) -> None:
        answer = {"note": "Onze anos de garantia"}

        assert not subset_match({"note": {"not_imatches": "onze"}}, answer)
        assert subset_match({"note": {"not_matches": "onze"}}, answer)
        assert subset_match({"note": {"not_imatches": "seis"}}, answer)

    @pytest.mark.parametrize("actual", [None, {"a": 1}, ["abc"]])
    def test_a_value_with_no_text_form_fails_both(self, actual: object) -> None:
        assert not subset_match({"v": {"imatches": ".*"}}, {"v": actual})
        assert not subset_match({"v": {"not_imatches": "x"}}, {"v": actual})

    @pytest.mark.parametrize("key", ["imatches", "not_imatches"])
    def test_invalid_regex_is_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match=rf"params.note: invalid regex in '{key}'"):
            validate_expected({"note": {key: "("}}, "params")

    @pytest.mark.parametrize("key", ["imatches", "not_imatches"])
    def test_a_pattern_has_to_be_text(self, key: str) -> None:
        with pytest.raises(ValueError, match=rf"'{key}' needs a regex string"):
            validate_expected({"note": {key: 123}}, "params")


class TestNotMatches:
    def test_not_matches_in_params_and_on_a_non_text_value(self) -> None:
        assert subset_match({"note": {"not_matches": "[0-9]{16}"}}, {"note": "card on file"})
        assert not subset_match(
            {"note": {"not_matches": "[0-9]{16}"}}, {"note": "card 4111111111111111"}
        )
        # Like the other negatives: a value it cannot read is a failure, not a pass.
        assert not subset_match({"note": {"not_matches": "x"}}, {"note": None})
        assert not subset_match({"note": {"not_matches": "x"}}, {"note": {"a": 1}})

    def test_invalid_regex_in_not_matches_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"params.note: invalid regex in 'not_matches'"):
            validate_expected({"note": {"not_matches": "("}}, "params")


class TestComparisons:
    """HB-7: lt, lte, gt, gte compare numbers, and only numbers."""

    def test_each_comparison_holds_on_its_side_of_the_bound(self) -> None:
        assert subset_match({"n": {"lt": 3}}, {"n": 2})
        assert not subset_match({"n": {"lt": 3}}, {"n": 3})
        assert subset_match({"n": {"lte": 3}}, {"n": 3})
        assert not subset_match({"n": {"lte": 3}}, {"n": 4})
        assert subset_match({"n": {"gt": 3}}, {"n": 4})
        assert not subset_match({"n": {"gt": 3}}, {"n": 3})
        assert subset_match({"n": {"gte": 3}}, {"n": 3})
        assert not subset_match({"n": {"gte": 3}}, {"n": 2})

    def test_ints_and_floats_compare_with_each_other(self) -> None:
        assert subset_match({"tokens": {"lte": 6000}}, {"tokens": 5999.5})
        assert subset_match({"score": {"gte": 0.5}}, {"score": 1})

    def test_a_range_is_two_conditions(self) -> None:
        bounded = {"limit": {"gte": 1, "lte": 3}}

        assert subset_match(bounded, {"limit": 2})
        assert not subset_match(bounded, {"limit": 0})
        assert not subset_match(bounded, {"limit": 4})

    @pytest.mark.parametrize("key", ["lt", "lte", "gt", "gte"])
    @pytest.mark.parametrize("actual", ["2", None, True, False, [1], {"n": 1}, float("nan")])
    def test_hb7_a_value_that_is_not_a_number_fails(self, key: str, actual: object) -> None:
        """A numeric string, a bool or a missing value is not evidence of a bound.

        `True < 3` is true in Python, and `"2"` is what a model writes when the schema
        says integer: either would make a bound green on a value it never checked.
        """
        assert not subset_match({"n": {key: 3}}, {"n": actual})

    def test_hb7_a_missing_key_fails(self) -> None:
        assert not subset_match({"n": {"lte": 3}}, {"other": 1})


class TestLengths:
    """HB-7: len and len_lte measure lists, and only lists."""

    def test_len_is_the_exact_length(self) -> None:
        assert subset_match({"docs": {"len": 2}}, {"docs": ["a", "b"]})
        assert not subset_match({"docs": {"len": 2}}, {"docs": ["a"]})
        assert subset_match({"docs": {"len": 0}}, {"docs": []})

    def test_len_lte_is_a_ceiling(self) -> None:
        assert subset_match({"docs": {"len_lte": 3}}, {"docs": [1, 2, 3]})
        assert subset_match({"docs": {"len_lte": 3}}, {"docs": []})
        assert not subset_match({"docs": {"len_lte": 3}}, {"docs": [1, 2, 3, 4]})

    @pytest.mark.parametrize("key", ["len", "len_lte"])
    @pytest.mark.parametrize("actual", ["ab", None, {"a": 1, "b": 2}, 2])
    def test_hb7_a_value_that_is_not_a_list_fails(self, key: str, actual: object) -> None:
        """A string has a length too, but a list that came as a string is the defect."""
        assert not subset_match({"docs": {key: 2}}, {"docs": actual})

    def test_combines_with_contains(self) -> None:
        expected = {"ids": {"len_lte": 3, "contains": "t-7"}}

        assert subset_match(expected, {"ids": ["t-1", "t-7"]})
        assert not subset_match(expected, {"ids": ["t-1", "t-2"]})


class TestComparisonOperands:
    """HB-7: a bound that is not a number fails at `highbar validate`."""

    @pytest.mark.parametrize("key", ["lt", "lte", "gt", "gte"])
    @pytest.mark.parametrize("operand", [3, 0, -1, 2.5])
    def test_accepts_a_number(self, key: str, operand: object) -> None:
        validate_expected({"n": {key: operand}}, "params")

    @pytest.mark.parametrize("key", ["lt", "lte", "gt", "gte"])
    @pytest.mark.parametrize(
        ("operand", "kind"), [("3", "str"), (True, "bool"), (None, "NoneType"), ([3], "list")]
    )
    def test_hb7_rejects_a_bound_that_is_not_a_number(
        self, key: str, operand: object, kind: str
    ) -> None:
        with pytest.raises(ValueError, match=rf"params\.n: '{key}' needs a number, got {kind}"):
            validate_expected({"n": {key: operand}}, "params")

    @pytest.mark.parametrize("key", ["len", "len_lte"])
    @pytest.mark.parametrize("operand", [0, 3])
    def test_accepts_a_whole_length(self, key: str, operand: int) -> None:
        validate_expected({"docs": {key: operand}}, "result")

    @pytest.mark.parametrize("key", ["len", "len_lte"])
    @pytest.mark.parametrize("operand", [-1, 2.5, "3", True, None])
    def test_hb7_rejects_a_length_that_is_not_a_whole_number(
        self, key: str, operand: object
    ) -> None:
        with pytest.raises(ValueError, match=rf"result\.docs: '{key}' needs a whole number"):
            validate_expected({"docs": {key: operand}}, "result")

    def test_hb7_a_literal_dict_with_these_keys_is_a_matcher_now(self) -> None:
        """`{equals: {...}}` is the way to compare such a dict as a literal."""
        assert not subset_match({"range": {"lt": 5}}, {"range": {"lt": 5}})
        assert subset_match({"range": {"equals": {"lt": 5}}}, {"range": {"lt": 5}})

    @pytest.mark.parametrize("key", ["lt", "lte", "gt", "gte"])
    @pytest.mark.parametrize("operand", [float("inf"), float("-inf"), float("nan")])
    def test_hb7_rejects_a_bound_that_is_not_finite(self, key: str, operand: float) -> None:
        """`lte: .inf` holds for every number: a check that cannot fail."""
        with pytest.raises(ValueError, match=rf"params\.n: '{key}' needs a finite number"):
            validate_expected({"n": {key: operand}}, "params")


class TestHugeBounds:
    def test_hb7_an_int_beyond_float_range_is_a_valid_bound(self) -> None:
        huge = int("9" * 400)

        validate_expected({"n": {"lte": huge}}, "params")
        assert subset_match({"n": {"lte": huge}}, {"n": 10**300})
        assert not subset_match({"n": {"gt": huge}}, {"n": 1.0})


class TestOneOf:
    """HB-7: `in` holds when the value equals one of the listed values."""

    def test_in_is_one_of(self) -> None:
        assert subset_match({"plan": {"in": ["basic", "pro"]}}, {"plan": "pro"})
        assert not subset_match({"plan": {"in": ["basic", "pro"]}}, {"plan": "Pro"})
        assert subset_match({"n": {"in": [1, 2]}}, {"n": 2})

    def test_in_compares_whole_values(self) -> None:
        assert subset_match({"opts": {"in": [{"size": "L"}]}}, {"opts": {"size": "L"}})
        assert not subset_match({"opts": {"in": [{"size": "L"}]}}, {"opts": {"size": "L", "x": 1}})

    def test_hb7_in_fails_on_a_missing_value(self) -> None:
        assert not subset_match({"plan": {"in": ["basic", "pro"]}}, {"other": "pro"})


class TestContainsAll:
    """HB-7: `contains_all` is `contains` for each item of its list."""

    def test_on_a_list_every_item_is_there(self) -> None:
        expected = {"ids": {"contains_all": ["t-1", "t-7"]}}

        assert subset_match(expected, {"ids": ["t-7", "t-2", "t-1"]})
        assert not subset_match(expected, {"ids": ["t-7", "t-2"]})

    def test_on_text_every_piece_is_there(self) -> None:
        expected = {"note": {"contains_all": ["refund", "1042"]}}

        assert subset_match(expected, {"note": "refund for order 1042"})
        assert not subset_match(expected, {"note": "refund for order 1043"})

    @pytest.mark.parametrize("actual", [None, {"a": "refund"}])
    def test_hb7_a_value_it_cannot_read_fails(self, actual: object) -> None:
        assert not subset_match({"note": {"contains_all": ["refund"]}}, {"note": actual})


class TestEachItem:
    """HB-7: `any` and `all` match the items of a list, like a value of params."""

    def test_any_needs_one_item_that_matches(self) -> None:
        docs = {
            "documents": [
                {"id": "t-1", "chunks": [0, 1, 2]},
                {"id": "t-7", "chunks": [3]},
            ]
        }

        assert subset_match({"documents": {"any": {"id": "t-1", "chunks": [0, 1, 2]}}}, docs)
        assert not subset_match({"documents": {"any": {"id": "t-1", "chunks": [0, 1]}}}, docs)
        assert subset_match({"documents": {"any": {"id": {"matches": "^t-[0-9]$"}}}}, docs)

    def test_all_needs_every_item_to_match(self) -> None:
        expected = {"documents": {"all": {"status": "published"}}}

        assert subset_match(
            expected, {"documents": [{"status": "published"}, {"status": "published", "n": 1}]}
        )
        assert not subset_match(
            expected, {"documents": [{"status": "published"}, {"status": "draft"}]}
        )

    def test_items_can_be_scalars_and_matchers(self) -> None:
        assert subset_match({"scores": {"all": {"gte": 0.5}}}, {"scores": [0.5, 0.9]})
        assert not subset_match({"scores": {"all": {"gte": 0.5}}}, {"scores": [0.5, 0.4]})
        assert subset_match({"tags": {"any": {"icontains": "drink"}}}, {"tags": ["Cold Drink"]})

    def test_hb7_all_on_an_empty_list_fails(self) -> None:
        """Every item of nothing matched, and nothing was checked: not evidence."""
        assert not subset_match({"documents": {"all": {"status": "published"}}}, {"documents": []})

    @pytest.mark.parametrize("key", ["any", "all"])
    @pytest.mark.parametrize("actual", [None, "published", {"status": "published"}])
    def test_hb7_a_value_that_is_not_a_list_fails(self, key: str, actual: object) -> None:
        assert not subset_match(
            {"documents": {key: {"status": "published"}}}, {"documents": actual}
        )

    def test_combines_with_a_length(self) -> None:
        expected = {"documents": {"len_lte": 3, "all": {"status": "published"}}}

        assert subset_match(expected, {"documents": [{"status": "published"}]})
        assert not subset_match(expected, {"documents": [{"status": "published"}] * 4})


class TestListOperands:
    """HB-7: the operands of in, contains_all, any and all are checked at load time."""

    @pytest.mark.parametrize("key", ["in", "contains_all"])
    @pytest.mark.parametrize("operand", [[], "pro", None, {"a": 1}])
    def test_hb7_in_and_contains_all_need_a_non_empty_list(self, key: str, operand: object) -> None:
        """`in: []` can never hold, and `contains_all: []` always does."""
        with pytest.raises(ValueError, match=rf"params\.plan: '{key}' needs a non-empty list"):
            validate_expected({"plan": {key: operand}}, "params")

    @pytest.mark.parametrize("key", ["any", "all"])
    def test_hb7_a_bad_matcher_inside_any_or_all_is_found(self, key: str) -> None:
        with pytest.raises(ValueError, match=rf"result\.documents\.{key}\.id: invalid regex"):
            validate_expected({"documents": {key: {"id": {"matches": "("}}}}, "result")

    @pytest.mark.parametrize("key", ["any", "all"])
    def test_hb7_a_misspelled_matcher_inside_any_or_all_is_found(self, key: str) -> None:
        with pytest.raises(ValueError, match="did you mean 'contains'"):
            validate_expected({"documents": {key: {"id": {"contain": "t-"}}}}, "result")

    def test_well_formed_list_matchers_pass(self) -> None:
        validate_expected(
            {
                "plan": {"in": ["basic", "pro"]},
                "ids": {"contains_all": ["t-1"]},
                "documents": {"any": {"id": "t-1"}, "all": {"chunks": {"len_lte": 3}}},
            },
            "result",
        )


class TestOperandsThatCannotFail:
    """Review of #27: an operand that holds on any input is refused at load time."""

    @pytest.mark.parametrize("key", ["any", "all"])
    def test_hb7_any_or_all_of_an_empty_object_is_refused(self, key: str) -> None:
        """`all: {}` only says "these are objects": green on any list of objects."""
        with pytest.raises(ValueError, match=rf"result\.documents: '{key}' needs something"):
            validate_expected({"documents": {key: {}}}, "result")

    def test_hb7_an_empty_piece_in_contains_all_is_refused(self) -> None:
        """ "" is in every text, so it adds a check that cannot fail."""
        with pytest.raises(ValueError, match=r"params\.q: 'contains_all' has an empty text"):
            validate_expected({"q": {"contains_all": ["refund", ""]}}, "params")

    @pytest.mark.parametrize("key", ["any", "all"])
    def test_hb7_an_empty_object_deeper_inside_any_or_all_is_refused(self, key: str) -> None:
        with pytest.raises(ValueError, match=rf"'{key}' needs something"):
            validate_expected({"documents": {"any": {"tags": {key: {}}}}}, "result")
