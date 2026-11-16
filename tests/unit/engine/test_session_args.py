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


TURN_WITH_ARG = (
    "      - session: {args: {order: 3001}}\n"
    "        turns:\n"
    '          - trigger: "hi"\n'
    '            expect: {response: {contains: "ok"}}\n'
    '          - trigger: "order {{test.args.order}}?"\n'
    '            expect: {response: {contains: "ok"}}\n'
)


class TestNotInATrigger:
    """A trigger goes to the agent as written: an arg there would be sent as braces."""

    @pytest.mark.parametrize(
        ("tests", "at", "template"),
        [
            (
                _test("order {{test.args.order}}?", "        session: {args: {order: 3001}}\n"),
                "suites[0].tests[0].trigger",
                "{{test.args.order}}",
            ),
            (TURN_WITH_ARG, "suites[0].tests[0].turns[1].trigger", "{{test.args.order}}"),
            (_test("say {{ test.args }}"), "suites[0].tests[0].trigger", "{{ test.args }}"),
        ],
        ids=["a-trigger", "a-turn", "no-path"],
    )
    def test_hb9_an_arg_in_a_trigger_is_refused_at_load(
        self, tmp_path: Path, tests: str, at: str, template: str
    ) -> None:
        with pytest.raises(SpecError) as caught:
            parse_spec(_spec(tmp_path, WITH_SESSION, _suites(tests)))

        assert str(caught.value).endswith(
            f"{at}: '{template}' is not read in a trigger: "
            "use it in agent.request, agent.correlate or a session hook"
        )

    def test_hb9_other_braces_in_a_trigger_stay_text(self, tmp_path: Path) -> None:
        """Names highbar does not own are the agent's text, as before."""
        (test,) = (
            parse_spec(
                _spec(tmp_path, WITH_SESSION, _suites(_test("hello {{name}} {{test.argsx}}")))
            )
            .suites[0]
            .tests
        )

        assert test.trigger == "hello {{name}} {{test.argsx}}"


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


class TestLevels:
    """HB-9 v1: args in defaults and in a suite; the most specific level wins, key by key."""

    def test_hb9_defaults_args_reach_every_test(self, tmp_path: Path) -> None:
        body = "defaults:\n  session: {args: {stage: open}}\n" + _suites(
            _test("a"), _test("b", "        session: {args: {stage: closed}}\n")
        )

        first, second = parse_spec(_spec(tmp_path, WITH_SESSION, body)).suites[0].tests

        assert (first.session_args, second.session_args) == ({"stage": "open"}, {"stage": "closed"})

    def test_hb9_a_suite_over_defaults_and_a_test_over_its_suite(self, tmp_path: Path) -> None:
        body = (
            "defaults:\n  session: {args: {a: 1, b: 1}}\n"
            "suites:\n  - name: S\n    session: {args: {b: 2, c: 2}}\n    tests:\n"
            + _test("a", "        session: {args: {c: 3}}\n")
            + "  - name: T\n    tests:\n"
            + _test("b")
        )

        s, t = parse_spec(_spec(tmp_path, WITH_SESSION, body)).suites

        assert s.tests[0].session_args == {"a": 1, "b": 2, "c": 3}
        assert t.tests[0].session_args == {"a": 1, "b": 1}

    def test_hb9_a_key_is_replaced_whole_not_merged(self, tmp_path: Path) -> None:
        """No deep merge: what a test's args are can be read from one place per key."""
        body = "defaults:\n  session: {args: {customer: {id: 7, name: Ana}}}\n" + _suites(
            _test("a", "        session: {args: {customer: {id: 8}}}\n")
        )

        test = parse_spec(_spec(tmp_path, WITH_SESSION, body)).suites[0].tests[0]

        assert test.session_args == {"customer": {"id": 8}}

    def test_hb9_the_load_check_reads_the_combined_args(self, tmp_path: Path) -> None:
        agent = WITH_SESSION + 'correlate: {s: "{{test.args.stage}}"}\n'
        body = "defaults:\n  session: {args: {stage: open}}\n" + _suites(_test("a"), _test("b"))

        parse_spec(_spec(tmp_path, agent, body))

        with pytest.raises(
            SpecError, match=r"suites\[0\]\.tests\[0\]\.session\.args: agent\.correlate\.s"
        ):
            parse_spec(_spec(tmp_path, agent, _suites(_test("a"))))

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                "defaults:\n  session: {args: {when: 2026-11-15}}\n" + _suites(_test("a")),
                r"defaults\.session\.args\.when: a date is not JSON",
            ),
            (
                "suites:\n  - name: S\n    session: {argz: {a: 1}}\n    tests:\n" + _test("a"),
                r"suites\[0\]\.session: unknown key 'argz' — did you mean 'args'",
            ),
        ],
    )
    def test_hb9_args_at_each_level_are_checked_the_same(
        self, tmp_path: Path, body: str, message: str
    ) -> None:
        with pytest.raises(SpecError, match=message):
            parse_spec(_spec(tmp_path, WITH_SESSION, body))

    def test_hb9_inherited_args_hash_as_if_written_in_the_test(self, tmp_path: Path) -> None:
        inherited = "defaults:\n  session: {args: {stage: open}}\n" + _suites(_test("a"))
        written = _suites(_test("a", "        session: {args: {stage: open}}\n"))

        (one,) = parse_spec(_spec(tmp_path, WITH_SESSION, inherited)).suites[0].tests
        (other,) = parse_spec(_spec(tmp_path, WITH_SESSION, written)).suites[0].tests

        assert one.definition_sha256 == other.definition_sha256
