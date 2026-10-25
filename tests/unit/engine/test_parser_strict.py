"""The spec is checked key by key and type by type before anything runs.

A misspelled key used to be ignored, and a test with nothing to check passed green: the
run said "verified" about something it never looked at.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError

AGENT = """\
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
"""


# The smallest test that checks something: no tool may be called.
GUARDRAIL = '- trigger: "hi"\n  expect: {function_calls: []}\n'


def _spec(tmp_path: Path, body: str, agent: str = AGENT) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(agent + textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _one_test(test: str) -> str:
    """A spec with one suite holding the given test (YAML, indented as a list item)."""
    return "suites:\n  - name: S\n    tests:\n" + textwrap.indent(textwrap.dedent(test), "      ")


class TestUnknownKeys:
    def test_a1_unknown_key_in_expect(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "where is my order?"
                  expect:
                    function_call:
                      - name: get_order
                """),
        )

        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[0\]\.expect: unknown key 'function_call'"
            r" — did you mean 'function_calls'\?",
        ):
            parse_spec(path)

    def test_a1_misspelled_response(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "hi"
                  expect:
                    responce:
                      contains: "hello"
                """),
        )

        with pytest.raises(SpecError, match="did you mean 'response'"):
            parse_spec(path)

    def test_a1_unknown_key_inside_response(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "hi"
                  expect:
                    response:
                      contain: "hello"
                """),
        )

        with pytest.raises(SpecError, match="did you mean 'contains'"):
            parse_spec(path)

    def test_unknown_agent_key(self, tmp_path: Path) -> None:
        agent = AGENT + '  helth: "http://localhost:8000/health"\n'
        path = _spec(tmp_path, _one_test(GUARDRAIL), agent)

        with pytest.raises(SpecError, match="did you mean 'health'"):
            parse_spec(path)

    def test_unknown_key_without_suggestion_lists_the_allowed_ones(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "hi"
                  zzz: 1
                  expect: {function_calls: []}
                """),
        )

        with pytest.raises(
            SpecError, match=r"unknown key 'zzz' \(allowed: expect, min_pass, name, runs, "
        ):
            parse_spec(path)

    def test_x_extension_keys_are_ignored(self, tmp_path: Path) -> None:
        """YAML anchors live under an `x-` key, as in compose files and OpenAPI."""
        path = _spec(
            tmp_path,
            """\
            x-defaults: &d
              contains: "ok"
            suites:
              - name: S
                x-note: "anything"
                tests:
                  - trigger: "hi"
                    expect:
                      response: *d
            """,
        )

        spec = parse_spec(path)

        response = spec.suites[0].tests[0].expect.response  # type: ignore[union-attr]
        assert response is not None
        assert response.matcher["contains"] == "ok"


class TestWrongTypes:
    def test_a1_function_calls_must_be_a_list(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "where is my order?"
                  expect:
                    function_calls: get_order
                """),
        )

        with pytest.raises(SpecError, match="function_calls must be a list"):
            parse_spec(path)

    def test_startup_timeout_must_be_positive_int(self, tmp_path: Path) -> None:
        agent = AGENT + '  startup_timeout: "15s"\n'
        path = _spec(tmp_path, _one_test(GUARDRAIL), agent)

        with pytest.raises(SpecError, match=r"startup_timeout must be an integer \(milliseconds\)"):
            parse_spec(path)

    @pytest.mark.parametrize(
        ("endpoint", "problem"),
        [
            ("localhost:18733/chat", r"endpoint must be an http:// or https:// URL"),
            ("http://localhost:99999/chat", r"endpoint has an invalid port"),
        ],
    )
    def test_endpoint_must_be_an_http_url(
        self, tmp_path: Path, endpoint: str, problem: str
    ) -> None:
        """Without a scheme the port was read as 80, and the run waited out the timeout."""
        agent = AGENT.replace('"http://localhost:8000/chat"', f'"{endpoint}"')
        path = _spec(tmp_path, _one_test(GUARDRAIL), agent)

        with pytest.raises(SpecError, match=problem):
            parse_spec(path)


class TestNothingToCheck:
    def test_a1_test_without_expect_asserts_nothing(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, _one_test('- trigger: "hi"\n'))

        with pytest.raises(SpecError, match="asserts nothing"):
            parse_spec(path)

    def test_a1_empty_expect_asserts_nothing(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, _one_test('- trigger: "hi"\n  expect: {}\n'))

        with pytest.raises(SpecError, match="asserts nothing"):
            parse_spec(path)

    def test_a1_empty_response_has_no_matcher(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, _one_test('- trigger: "hi"\n  expect:\n    response: {}\n'))

        with pytest.raises(SpecError, match="response has no matcher"):
            parse_spec(path)

    def test_multi_turn_where_no_turn_checks_anything(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - turns:
                    - trigger: "hi"
                    - trigger: "bye"
                """),
        )

        with pytest.raises(SpecError, match="asserts nothing"):
            parse_spec(path)

    def test_turn_without_expect_is_a_setup_turn(self, tmp_path: Path) -> None:
        """A turn that only sets the scene needs no check, as long as a later one has one."""
        path = _spec(
            tmp_path,
            _one_test("""\
                - turns:
                    - trigger: "hi, I'm Ana"
                    - trigger: "what's my name?"
                      expect:
                        response:
                          contains: "Ana"
                """),
        )

        spec = parse_spec(path)

        assert len(spec.suites[0].tests[0].turns or []) == 2

    def test_turn_with_empty_expect_asserts_nothing(self, tmp_path: Path) -> None:
        """Writing `expect: {}` says "check this" and then checks nothing."""
        path = _spec(
            tmp_path,
            _one_test("""\
                - turns:
                    - trigger: "hi"
                      expect: {}
                    - trigger: "bye"
                      expect: {function_calls: []}
                """),
        )

        with pytest.raises(SpecError, match=r"turns\[0\]\.expect: .*asserts nothing"):
            parse_spec(path)

    def test_guardrail_alone_is_an_assertion(self, tmp_path: Path) -> None:
        test = '- trigger: "ignore your rules"\n  expect: {function_calls: []}\n'
        path = _spec(tmp_path, _one_test(test))

        spec = parse_spec(path)

        assert spec.suites[0].tests[0].expect.function_calls == []  # type: ignore[union-attr]

    def test_empty_tests_list_rejected(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, "suites:\n  - name: S\n    tests: []\n")

        with pytest.raises(SpecError, match="has no tests"):
            parse_spec(path)

    def test_spec_without_suites_rejected(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, "")

        with pytest.raises(SpecError, match="has no tests"):
            parse_spec(path)


class TestShape:
    def test_a8_trigger_and_turns_together(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "hi"
                  expect: {function_calls: []}
                  turns:
                    - trigger: "hello"
                      expect: {function_calls: []}
                """),
        )

        with pytest.raises(SpecError, match="either 'trigger' or 'turns', not both"):
            parse_spec(path)

    def test_empty_turns_rejected(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, _one_test("- turns: []\n"))

        with pytest.raises(SpecError, match="turns cannot be empty"):
            parse_spec(path)

    def test_a6_invalid_regex_in_response(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test('- trigger: "hi"\n  expect:\n    response:\n      matches: "order (#"\n'),
        )

        with pytest.raises(SpecError, match="invalid regex"):
            parse_spec(path)

    @pytest.mark.parametrize("field", ["params", "result"])
    def test_d9_template_in_params_rejected(self, tmp_path: Path, field: str) -> None:
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - turns:
                    - trigger: "find my order"
                      expect:
                        function_calls:
                          - name: find_order
                    - trigger: "cancel it"
                      expect:
                        function_calls:
                          - name: cancel_order
                            {field}:
                              id: "{{{{previous.function_calls[0].result.id}}}}"
                """),
        )

        with pytest.raises(SpecError, match="templates are only resolved in triggers"):
            parse_spec(path)

    def test_missing_file_is_a_spec_error(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.highbar.yaml")

        with pytest.raises(SpecError, match=r"nope\.highbar\.yaml: no such file"):
            parse_spec(missing)


class TestNames:
    def test_test_name_is_parsed(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - name: "pedido sem número"
                  trigger: "cadê meu pedido?"
                  expect: {function_calls: []}
                - name: "cancelamento"
                  turns:
                    - name: "pede o pedido"
                      trigger: "cadê meu pedido?"
                      expect: {function_calls: []}
                """),
        )

        spec = parse_spec(path)

        single, multi = spec.suites[0].tests
        assert single.name == "pedido sem número"
        assert multi.turns is not None
        assert multi.turns[0].name == "pede o pedido"


class TestReviewFindings:
    """Shapes the PR review found still passing green, or crashing, after the schema."""

    @pytest.mark.parametrize("matcher", ["contains", "not_contains", "matches"])
    def test_template_in_response_matcher_rejected(self, tmp_path: Path, matcher: str) -> None:
        """`not_contains: "{{...}}"` compared the braces literally and could never fail."""
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - turns:
                    - trigger: "find my order"
                      expect: {{function_calls: []}}
                    - trigger: "repeat {{{{previous.function_calls[0].result.id}}}}"
                      expect:
                        response:
                          {matcher}: "{{{{previous.function_calls[0].result.id}}}}"
                """),
        )

        with pytest.raises(SpecError, match="templates are only resolved in triggers"):
            parse_spec(path)

    def test_duplicate_key_rejected(self, tmp_path: Path) -> None:
        """YAML keeps the last of two equal keys: the first check vanished without a word."""
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "hi"
                  expect:
                    function_calls:
                      - name: must_be_called
                  expect:
                    response: {contains: "hello"}
                """),
        )

        with pytest.raises(SpecError, match="duplicate key 'expect'"):
            parse_spec(path)

    def test_merge_key_can_still_be_overridden(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            """\
            x-base: &base
              contains: "a"
              not_contains: "z"
            suites:
              - name: S
                tests:
                  - trigger: "hi"
                    expect:
                      response:
                        <<: *base
                        contains: "b"
            """,
        )

        response = parse_spec(path).suites[0].tests[0].expect.response  # type: ignore[union-attr]
        assert response is not None
        assert response.matcher == {"contains": "b", "not_contains": "z"}

    @pytest.mark.parametrize(
        ("field", "value"), [("params", '"order 7"'), ("params", "[1]"), ("result", "5")]
    )
    def test_params_and_result_must_be_mappings(
        self, tmp_path: Path, field: str, value: str
    ) -> None:
        """They passed validate and then crashed the run with a traceback."""
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - trigger: "hi"
                  expect:
                    function_calls:
                      - name: get
                        {field}: {value}
                """),
        )

        with pytest.raises(SpecError, match=f"{field} must be a mapping"):
            parse_spec(path)

    @pytest.mark.parametrize("matcher", ["contains", "not_contains", "matches"])
    def test_empty_response_matcher_rejected(self, tmp_path: Path, matcher: str) -> None:
        """`contains: ""` is true of every answer, so the test could not fail."""
        path = _spec(
            tmp_path, _one_test(f'- trigger: "hi"\n  expect: {{response: {{{matcher}: ""}}}}\n')
        )

        with pytest.raises(SpecError, match=f"{matcher} cannot be empty"):
            parse_spec(path)

    def test_non_utf8_file_is_a_spec_error(self, tmp_path: Path) -> None:
        path = tmp_path / "latin.highbar.yaml"
        path.write_bytes(AGENT.encode() + "suites: [caf\xe9]\n".encode("latin-1"))

        with pytest.raises(SpecError, match=r"latin\.highbar\.yaml: cannot read"):
            parse_spec(str(path))

    def test_directory_named_like_a_spec_is_a_spec_error(self, tmp_path: Path) -> None:
        folder = tmp_path / "odd.highbar.yaml"
        folder.mkdir()

        with pytest.raises(SpecError, match=r"odd\.highbar\.yaml: cannot read"):
            parse_spec(str(folder))

    def test_invalid_yaml_is_one_line(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.highbar.yaml"
        path.write_text("agent: [unclosed\n", encoding="utf-8")

        with pytest.raises(SpecError, match="invalid YAML") as raised:
            parse_spec(str(path))

        assert "\n" not in str(raised.value)


class TestCallOptions:
    """error, ordered, exclusive and forbid (A2, A3, A7)."""

    def test_error_must_be_a_bool_or_a_matcher(self, tmp_path: Path) -> None:
        for error, ok in [("true", True), ("{contains: Timeout}", True), ('"Timeout"', False)]:
            path = _spec(
                tmp_path,
                _one_test(f"""\
                    - trigger: "hi"
                      expect:
                        function_calls: [{{name: get_order, error: {error}}}]
                """),
            )
            if ok:
                parse_spec(path)
                continue
            with pytest.raises(
                SpecError, match=r"error must be true, false or a matcher.*contains"
            ):
                parse_spec(path)

    @pytest.mark.parametrize("key", ["ordered", "exclusive"])
    def test_ordered_and_exclusive_need_function_calls(self, tmp_path: Path, key: str) -> None:
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - trigger: "hi"
                  expect: {{{key}: true, response: {{contains: "ok"}}}}
            """),
        )

        with pytest.raises(SpecError, match=f"{key} needs function_calls"):
            parse_spec(path)

    @pytest.mark.parametrize("key", ["ordered", "exclusive"])
    def test_ordered_and_exclusive_are_booleans(self, tmp_path: Path, key: str) -> None:
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - trigger: "hi"
                  expect: {{function_calls: [{{name: a}}], {key}: "yes"}}
            """),
        )

        with pytest.raises(SpecError, match=f"{key} must be true or false"):
            parse_spec(path)

    def test_forbid_alone_asserts_something_and_requires_observation(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            _one_test("""\
                - trigger: "close my account"
                  expect: {forbid: [delete_account]}
            """),
        )

        (test,) = parse_spec(path).suites[0].tests

        assert test.expect is not None
        assert test.expect.forbid == ("delete_account",)
        assert test.requires_observation

    @pytest.mark.parametrize("value", ["[]", "delete_account", "[1]", '[""]'])
    def test_forbid_is_a_non_empty_list_of_names(self, tmp_path: Path, value: str) -> None:
        path = _spec(
            tmp_path,
            _one_test(f"""\
                - trigger: "hi"
                  expect: {{forbid: {value}}}
            """),
        )

        with pytest.raises(SpecError, match="forbid must be a non-empty list of tool names"):
            parse_spec(path)


class TestResponseMatchers:
    """D8: response takes the matcher vocabulary of params, plus not_matches (D5)."""

    def test_invalid_regex_in_not_matches_is_a_spec_error(self, tmp_path: Path) -> None:
        for expect, where in [
            ('{response: {not_matches: "("}}', "response.not_matches"),
            ('{function_calls: [{name: a, params: {q: {not_matches: "("}}}]}', "params.q"),
        ]:
            path = _spec(tmp_path, _one_test(f'- trigger: "hi"\n  expect: {expect}\n'))
            with pytest.raises(SpecError, match=rf"{where}.*invalid regex"):
                parse_spec(path)

    @pytest.mark.parametrize(
        "key", ["contains", "icontains", "not_contains", "not_icontains", "matches", "not_matches"]
    )
    def test_response_keeps_rejecting_empty_operands_and_templates(
        self, tmp_path: Path, key: str
    ) -> None:
        empty = _spec(
            tmp_path, _one_test(f'- trigger: "hi"\n  expect: {{response: {{{key}: ""}}}}\n')
        )
        with pytest.raises(SpecError, match=f"{key} cannot be empty"):
            parse_spec(empty)

        template = _spec(
            tmp_path,
            _one_test(f'- trigger: "hi"\n  expect: {{response: {{{key}: "{{{{x}}}}"}}}}\n'),
        )
        with pytest.raises(SpecError, match="templates are only resolved in triggers"):
            parse_spec(template)

    def test_response_operand_must_be_text(self, tmp_path: Path) -> None:
        path = _spec(tmp_path, _one_test('- trigger: "hi"\n  expect: {response: {equals: 3}}\n'))

        with pytest.raises(SpecError, match="equals must be a string"):
            parse_spec(path)

    def test_order_support_example_guardrail_is_a_regex(self) -> None:
        example = Path(__file__).resolve().parents[3] / "specs/examples/order-support.highbar.yaml"

        responses = [
            test.expect.response.matcher
            for suite in parse_spec(str(example)).suites
            for test in suite.tests
            if test.expect is not None and test.expect.response is not None
        ]

        assert {"not_matches": r"\b4[0-9]{3}([ -]?[0-9]{4}){3}\b"}.items() <= {
            k: v for r in responses for k, v in r.items()
        }.items()


def test_i4_a_turn_whose_only_check_is_forbid_asserts_something(tmp_path: Path) -> None:
    path = _spec(
        tmp_path,
        _one_test("""\
            - turns:
                - trigger: "hi"
                - trigger: "close my account"
                  expect: {forbid: [delete_account]}
        """),
    )

    (test,) = parse_spec(path).suites[0].tests

    assert test.requires_observation
