"""Parser validation of matcher dicts in function_call params/result."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec


def _spec_with_params(tmp_path: Path, params: str) -> str:
    spec = tmp_path / "test.highbar.yaml"
    spec.write_text(
        textwrap.dedent(
            f"""
            agent:
              start: "true"
              endpoint: "http://localhost:8000/chat"
            suites:
              - name: Products
                tests:
                  - trigger: "do you have coke?"
                    expect:
                      function_calls:
                        - name: search_products
                          params: {params}
            """
        )
    )
    return str(spec)


class TestMatcherValidation:
    def test_well_formed_matchers_parse(self, tmp_path: Path) -> None:
        path = _spec_with_params(
            tmp_path, '{product: {icontains: "coca"}, id: {matches: "^[0-9]+$"}}'
        )

        call = parse_spec(path).suites[0].tests[0].expect.function_calls[0]

        assert call.params == {"product": {"icontains": "coca"}, "id": {"matches": "^[0-9]+$"}}

    @pytest.mark.parametrize(
        ("params", "error"),
        [
            ('{id: {matches: "[0-9"}}', "invalid regex"),
            ('{product: {contain: "coca"}}', "did you mean 'contains'"),
            ('{product: {contains: "coca", size: "L"}}', "mixes matcher keys"),
        ],
    )
    def test_malformed_matcher_fails_at_parse_time(
        self, tmp_path: Path, params: str, error: str
    ) -> None:
        """Otherwise it raises re.error mid-run, or silently compares as a literal."""
        with pytest.raises(ValueError, match=error):
            parse_spec(_spec_with_params(tmp_path, params))


class TestBoundsAndLengths:
    """HB-7: lt, lte, gt, gte, len and len_lte in values of params and result."""

    def test_bounds_and_lengths_parse_in_params(self, tmp_path: Path) -> None:
        path = _spec_with_params(tmp_path, "{limit: {gte: 1, lte: 3}, ids: {len_lte: 3}}")

        call = parse_spec(path).suites[0].tests[0].expect.function_calls[0]

        assert call.params == {"limit": {"gte": 1, "lte": 3}, "ids": {"len_lte": 3}}

    @pytest.mark.parametrize(
        ("params", "error"),
        [
            ('{limit: {lte: "3"}}', r"params\.limit: 'lte' needs a number, got str"),
            ("{limit: {gt: true}}", r"'gt' needs a number, got bool"),
            ("{limit: {lte: .inf}}", r"'lte' needs a finite number"),
            ("{ids: {len: -1}}", r"params\.ids: 'len' needs a whole number"),
            ("{ids: {len_lte: 1.5}}", r"'len_lte' needs a whole number"),
        ],
    )
    def test_hb7_a_bad_bound_fails_at_parse_time(
        self, tmp_path: Path, params: str, error: str
    ) -> None:
        with pytest.raises(ValueError, match=error):
            parse_spec(_spec_with_params(tmp_path, params))


def _spec_with_expect(tmp_path: Path, expect: str) -> str:
    spec = tmp_path / "test.highbar.yaml"
    spec.write_text(
        textwrap.dedent(
            f"""
            agent:
              start: "true"
              endpoint: "http://localhost:8000/chat"
            suites:
              - name: S
                tests:
                  - trigger: "hi"
                    expect: {expect}
            """
        )
    )
    return str(spec)


class TestTextOnlyPlaces:
    """HB-7: a reply and an error are text, so a bound there could never hold."""

    @pytest.mark.parametrize("key", ["lt", "lte", "gt", "gte", "len", "len_lte"])
    def test_hb7_response_refuses_a_bound(self, tmp_path: Path, key: str) -> None:
        path = _spec_with_expect(tmp_path, f"{{response: {{{key}: 3}}}}")

        with pytest.raises(
            ValueError, match=rf"response: '{key}' does not apply to the reply, which is text"
        ):
            parse_spec(path)

    @pytest.mark.parametrize("key", ["lt", "len_lte"])
    def test_hb7_error_refuses_a_bound(self, tmp_path: Path, key: str) -> None:
        path = _spec_with_expect(
            tmp_path, f"{{function_calls: [{{name: get_order, error: {{{key}: 3}}}}]}}"
        )

        with pytest.raises(
            ValueError, match=rf"error: '{key}' does not apply to an error, which is text"
        ):
            parse_spec(path)
