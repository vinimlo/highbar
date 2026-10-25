"""Tests for {{previous.*}} template resolution."""

from typing import Any

import pytest

from highbar.domain.models import Trace
from highbar.engine.templates import TemplateError, resolve_template


def _trace(result: Any) -> Trace:
    return Trace(
        id="t1", seq=0, fn="find_order", args={}, result=result, started_at=1.0, ended_at=1.1
    )


class TestResolveTemplate:
    def test_no_template(self) -> None:
        assert resolve_template("hello world", []) == "hello world"

    def test_simple_result_access(self) -> None:
        traces = [
            Trace(
                id="t1",
                seq=0,
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
                id="t1",
                seq=0,
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
            Trace(
                id="t1", seq=0, fn="fn_a", args={}, result={"x": "A"}, started_at=1.0, ended_at=1.1
            ),
            Trace(
                id="t2", seq=1, fn="fn_b", args={}, result={"x": "B"}, started_at=1.0, ended_at=1.1
            ),
        ]
        template = "Value: {{previous.function_calls[1].result.x}}"
        result = resolve_template(template, traces)
        assert result == "Value: B"

    def test_function_name_access(self) -> None:
        traces = [
            Trace(
                id="t1",
                seq=0,
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
                id="t1",
                seq=0,
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
