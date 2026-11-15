"""Tests for {{previous.*}} template resolution."""

from typing import Any

import pytest

from highbar.domain.models import Trace
from highbar.engine.templates import TemplateError, resolve_template


def _trace(result: Any) -> Trace:
    return Trace(fn="find_order", args={}, result=result, started_at=1.0, ended_at=1.1)


class TestResolveTemplate:
    def test_no_template(self) -> None:
        assert resolve_template("hello world", []) == "hello world"

    def test_simple_result_access(self) -> None:
        traces = [
            Trace(
                fn="search_orders",
                args={},
                result={"order_id": "ORD-123"},
                started_at=1.0,
                ended_at=1.1,
            )
        ]
        template = "Refund {{previous.function_calls[0].result.order_id}}"
        result = resolve_template(template, traces)
        assert result == "Refund ORD-123"

    def test_nested_result(self) -> None:
        traces = [
            Trace(
                fn="get_user",
                args={},
                result={"user": {"name": "Alice", "email": "alice@example.com"}},
                started_at=1.0,
                ended_at=1.1,
            )
        ]
        template = "Email: {{previous.function_calls[0].result.user.email}}"
        result = resolve_template(template, traces)
        assert result == "Email: alice@example.com"

    def test_second_function_call(self) -> None:
        traces = [
            Trace(fn="fn_a", args={}, result={"x": "A"}, started_at=1.0, ended_at=1.1),
            Trace(fn="fn_b", args={}, result={"x": "B"}, started_at=1.0, ended_at=1.1),
        ]
        template = "Value: {{previous.function_calls[1].result.x}}"
        result = resolve_template(template, traces)
        assert result == "Value: B"

    def test_function_name_access(self) -> None:
        traces = [
            Trace(
                fn="search_orders",
                args={},
                result=None,
                started_at=1.0,
                ended_at=1.1,
            )
        ]
        template = "Called: {{previous.function_calls[0].fn}}"
        result = resolve_template(template, traces)
        assert result == "Called: search_orders"

    def test_multiple_templates_in_one_string(self) -> None:
        traces = [
            Trace(
                fn="lookup",
                args={},
                result={"id": "123", "name": "Test"},
                started_at=1.0,
                ended_at=1.1,
            )
        ]
        template = (
            "ID={{previous.function_calls[0].result.id}}"
            " NAME={{previous.function_calls[0].result.name}}"
        )
        result = resolve_template(template, traces)
        assert result == "ID=123 NAME=Test"

    def test_missing_index_raises(self) -> None:
        traces: list[Trace] = []
        template = "{{previous.function_calls[0].result.x}}"
        with pytest.raises(TemplateError):
            resolve_template(template, traces)


class TestHonestTemplates:
    def test_d15_spaces_inside_braces(self) -> None:
        template = "id {{ previous.function_calls[0].result.id }}"

        assert resolve_template(template, [_trace({"id": 7})]) == "id 7"

    def test_d15_list_value_renders_as_json(self) -> None:
        """A Python repr (['sneakers']) is not what anyone would type to the agent."""
        template = "{{previous.function_calls[0].result.items}}"

        assert resolve_template(template, [_trace({"items": ["sneakers"]})]) == '["sneakers"]'

    def test_dict_value_renders_as_json(self) -> None:
        template = "{{previous.function_calls[0].result}}"

        assert resolve_template(template, [_trace({"a": 1})]) == '{"a": 1}'

    def test_a5_missing_call_raises_template_error(self) -> None:
        template = "cancel {{previous.function_calls[0].result.id}}"

        with pytest.raises(TemplateError, match=r"function_calls\[0\].*only 0 calls"):
            resolve_template(template, [])

    def test_a5_missing_key_lists_available_keys(self) -> None:
        template = "{{previous.function_calls[0].result.nope}}"

        with pytest.raises(TemplateError, match=r"no key 'nope' \(keys: id\)"):
            resolve_template(template, [_trace({"id": 7})])

    def test_nested_indices_resolve(self) -> None:
        template = "{{previous.function_calls[0].result.m[1][0]}}"

        assert resolve_template(template, [_trace({"m": [[1, 2], [3, 4]]})]) == "3"

    def test_malformed_path_is_an_error(self) -> None:
        """`m[1]x` used to drop the `x` and resolve to m[1]: a wrong value, silently."""
        template = "{{previous.function_calls[0].result.m[1]x}}"

        with pytest.raises(TemplateError, match="invalid path"):
            resolve_template(template, [_trace({"m": [1, 2]})])


class TestPreviousResponse:
    """HB-20: a turn quotes what the agent said before ("that second one you said")."""

    def test_hb20_previous_response_is_the_last_turns_reply(self) -> None:
        template = "the second one you said: {{previous.response}}"

        assert resolve_template(template, [], ["hi", "A or B"]) == "the second one you said: A or B"

    def test_hb20_a_turn_by_its_index(self) -> None:
        template = "{{previous.turns[0].response}} / {{ previous.turns[1].response }}"

        assert resolve_template(template, [], ["first", "second"]) == "first / second"

    def test_hb20_the_first_turn_has_no_previous_response(self) -> None:
        with pytest.raises(TemplateError, match=r"\{\{previous\.response\}\}: no earlier turn"):
            resolve_template("{{previous.response}}", [])

    def test_hb20_a_reply_that_was_not_read_is_an_error_not_null(self) -> None:
        """Sending "null" would test a message nobody meant to write."""
        with pytest.raises(TemplateError, match=r"turn 2 has no response"):
            resolve_template("{{previous.response}}", [], ["hi", None])
        with pytest.raises(TemplateError, match=r"turn 1 has no response"):
            resolve_template("{{previous.turns[0].response}}", [], [None, "ok"])

    def test_hb20_a_turn_that_is_not_there(self) -> None:
        with pytest.raises(TemplateError, match=r"only 2 earlier turns"):
            resolve_template("{{previous.turns[2].response}}", [], ["a", "b"])

    def test_hb20_an_unknown_field_of_a_turn_says_what_there_is(self) -> None:
        with pytest.raises(TemplateError, match=r"no field 'reply' \(fields: response\)"):
            resolve_template("{{previous.turns[0].reply}}", [], ["a"])

    def test_hb20_an_unknown_name_lists_all_three(self) -> None:
        with pytest.raises(TemplateError, match=r"keys: function_calls, response, turns"):
            resolve_template("{{previous.reply}}", [], ["a"])

    @pytest.mark.parametrize("path", ["turns", "turns[0]", "turns[1]"])
    def test_hb20_a_turn_is_not_a_value_only_its_response_is(self, path: str) -> None:
        """Review of #33: a turn as JSON carried an unread reply out as "response": null."""
        template = "{{previous." + path + "}}"
        for replies in (["a", None], ["a", "b"]):
            with pytest.raises(TemplateError, match=r"end the path with \.response"):
                resolve_template(template, [], replies)

    def test_hb20_calls_still_resolve_next_to_responses(self) -> None:
        template = "{{previous.function_calls[0].result.id}} after {{previous.response}}"

        assert resolve_template(template, [_trace({"id": 7})], ["found it"]) == "7 after found it"
