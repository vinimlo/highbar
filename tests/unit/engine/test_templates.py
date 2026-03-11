"""Tests for {{previous.*}} template resolution."""

import pytest

from gauntlet.domain.models import Trace
from gauntlet.engine.templates import resolve_template


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
        with pytest.raises(IndexError):
            resolve_template(template, traces)
