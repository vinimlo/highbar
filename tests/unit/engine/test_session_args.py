"""HB-9: a test's own session arguments, for its hooks and its request.

A test can start from a state of its own (a contact whose data was already collected, a
conversation already closed) without a spec per state: ``session: {args: {...}}``.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from highbar.domain.models import PythonHook
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.placeholders import (
    ALL,
    SETUP,
    PlaceholderError,
    Values,
    check_placeholders,
    render,
)
from highbar.engine.schema import SpecError
from highbar.engine.session import SessionContext
from tests.unit.engine.test_parser_agent import MANAGED, _spec
from tests.unit.engine.test_session import SAYS_OK, _command, _FakeSession, _hooks, _run

WITH_SESSION = MANAGED + 'session:\n  setup: "open"\n'


def _suites(*tests: str) -> str:
    return "suites:\n  - name: S\n    tests:\n" + "".join(tests)


def _test(trigger: str, extra: str = "") -> str:
    return (
        f'      - trigger: "{trigger}"\n        expect: {{response: {{contains: "ok"}}}}\n' + extra
    )


ARGS = "        session: {args: {customer: {id: 7}, stage: closed}}\n"


class TestTheSpec:
    def test_hb9_a_test_takes_session_args(self, tmp_path: Path) -> None:
        spec = parse_spec(_spec(tmp_path, WITH_SESSION, _suites(_test("a", ARGS), _test("b"))))

        first, second = spec.suites[0].tests
        assert first.session_args == {"customer": {"id": 7}, "stage": "closed"}
        assert second.session_args == {}

    def test_hb9_a_multi_turn_test_takes_them_too(self, tmp_path: Path) -> None:
        suites = _suites(
            "      - session: {args: {stage: closed}}\n"
            "        turns:\n"
            '          - trigger: "hi"\n'
            '            expect: {response: {contains: "ok"}}\n'
        )

        assert parse_spec(_spec(tmp_path, WITH_SESSION, suites)).suites[0].tests[
            0
        ].session_args == {"stage": "closed"}

    @pytest.mark.parametrize(
        ("session", "message"),
        [
            ("{args: [1, 2]}", r"session\.args: must be a mapping"),
            ("{argz: {a: 1}}", r"session: unknown key 'argz' — did you mean 'args'"),
            ("{}", r"session: needs 'args'"),
            ("[1]", r"session: must be a mapping"),
            ("{args: {when: 2026-11-15}}", r"session\.args\.when: a date is not JSON"),
            ("{args: {n: .nan}}", r"session\.args\.n: nan is not JSON"),
            ("{args: {1: a}}", r"session\.args: the key 1 is not text"),
        ],
    )
    def test_hb9_args_are_a_json_object(self, tmp_path: Path, session: str, message: str) -> None:
        suites = _suites(_test("a", f"        session: {session}\n"))

        with pytest.raises(SpecError, match=rf"suites\[0\]\.tests\[0\]\.{message}"):
            parse_spec(_spec(tmp_path, WITH_SESSION, suites))

    def test_hb9_an_arg_the_agent_uses_must_be_in_every_test(self, tmp_path: Path) -> None:
        """Found at load, not when the second test runs and its request goes out short."""
        agent = (
            WITH_SESSION
            + 'request:\n  body: {message: "{{message}}", c: "{{test.args.customer.id}}"}\n'
        )

        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[1\]\.session\.args: agent\.request\.body\.c uses "
            r"'\{\{test\.args\.customer\.id\}\}': no key 'customer'",
        ):
            parse_spec(_spec(tmp_path, agent, _suites(_test("a", ARGS), _test("b"))))

    def test_hb9_an_arg_the_agent_uses_cannot_be_null(self, tmp_path: Path) -> None:
        agent = MANAGED + 'session:\n  setup: "open {{test.args.stage}}"\n'

        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[0\]\.session\.args: agent\.session\.setup uses "
            r"'\{\{test\.args\.stage\}\}': the value is null",
        ):
            parse_spec(
                _spec(
                    tmp_path, agent, _suites(_test("a", "        session: {args: {stage: null}}\n"))
                )
            )

    def test_hb9_args_the_agent_does_not_use_can_differ(self, tmp_path: Path) -> None:
        agent = WITH_SESSION + 'correlate: {c: "{{test.args.customer.id}}"}\n'
        other = "        session: {args: {customer: {id: 8}}}\n"

        parse_spec(_spec(tmp_path, agent, _suites(_test("a", ARGS), _test("b", other))))

    def test_hb9_args_change_what_the_test_measures(self) -> None:
        plain = Case(trigger="a", expect=SAYS_OK)

        assert Case(trigger="a", expect=SAYS_OK, session_args={}).definition_sha256 == (
            plain.definition_sha256
        )
        assert Case(trigger="a", expect=SAYS_OK, session_args={"k": 1}).definition_sha256 != (
            plain.definition_sha256
        )


class TestThePlaceholder:
    def test_hb9_a_test_arg_keeps_its_json_type_and_reads_paths(self) -> None:
        values = Values(
            message="m", test_id="t", test_name="n", session={}, args={"c": {"id": 7}, "t": ["x"]}
        )

        assert render({"id": "{{test.args.c.id}}", "q": "tag {{ test.args.t.0 }}"}, values) == {
            "id": 7,
            "q": "tag x",
        }

    def test_hb9_a_test_arg_that_is_not_there_names_the_keys(self) -> None:
        values = Values(message="m", test_id="t", test_name="n", session={}, args={"c": 1})

        with pytest.raises(PlaceholderError, match=r"^\{\{test\.args\.x\}\}: no key 'x'"):
            render("{{test.args.x}}", values)

    def test_hb9_the_setup_hook_can_use_them(self) -> None:
        """Unlike {{session.*}}: the args are known before the session opens."""
        check_placeholders(
            ["open", "{{test.args.stage}}"], "setup", allowed=SETUP, has_session=True
        )

    @pytest.mark.parametrize("placeholder", ["{{test.args}}", "{{test.args.}}"])
    def test_hb9_test_args_with_no_path_is_unknown(self, placeholder: str) -> None:
        with pytest.raises(SpecError, match="unknown placeholder"):
            check_placeholders(placeholder, "agent.request.body.x", allowed=ALL, has_session=False)


class TestTheRun:
    def test_hb9_each_execution_gets_its_tests_args(self) -> None:
        session = _FakeSession()
        tests = [
            Case(trigger="a", expect=SAYS_OK, session_args={"customer": {"id": 7}}),
            Case(trigger="b", expect=SAYS_OK),
        ]

        agent, _ = _run(tests, session)

        assert [dict(ctx.args) for ctx in session.setups] == [{"customer": {"id": 7}}, {}]
        assert [dict(values.args) for values in agent.values] == [{"customer": {"id": 7}}, {}]

    def test_hb9_a_command_hook_gets_an_arg_as_one_argument(self) -> None:
        hooks = _hooks(
            _command("import json, sys; print(json.dumps({'got': sys.argv[1]}))", "{{test.args.s}}")
        )
        ctx = SessionContext(test_id="a", test_name="n", spec_path="s", args={"s": "closed; ls"})

        assert hooks.setup(ctx) == {"got": "closed; ls"}

    def test_hb9_a_python_hook_reads_ctx_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delitem(sys.modules, "hb9_hooks", raising=False)
        (tmp_path / "hb9_hooks.py").write_text(
            textwrap.dedent("""\
                def open_session(ctx):
                    return {"stage": ctx.args["stage"]}
                """),
            encoding="utf-8",
        )
        hooks = _hooks(PythonHook("hb9_hooks:open_session"))
        ctx = SessionContext(test_id="a", test_name="n", spec_path="s", args={"stage": "closed"})

        assert hooks.setup(ctx) == {"stage": "closed"}
