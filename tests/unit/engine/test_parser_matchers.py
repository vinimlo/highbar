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
