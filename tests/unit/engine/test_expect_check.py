"""HB-12: ``expect.check``, a deterministic check of the author's own, in Python.

Some outcomes are not in the reply nor in a tool call: a list the agent must have
cleared, a row it must have written. The check is a function the spec names. It gets
what highbar knows of the turn and returns the failures it found: none is a pass. It
runs in the hooks' thread, after highbar's own checks of the turn.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from highbar.domain.models import (
    Expect,
    FunctionCallExpect,
    ResponseExpect,
    SessionConfig,
    Status,
    Trace,
    Turn,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError
from highbar.engine.session import CheckContext, SessionError, SessionHooks
from tests.unit.engine.test_outcomes import Replying, _run

CHECK = "evals_checks:no_open_trainings"
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))


def _parsed(tmp_path: Path, test: str) -> Case:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  start: "python agent.py"\n  endpoint: "http://localhost:8000/chat"\n'
        "suites:\n  - name: S\n    tests:\n" + textwrap.indent(textwrap.dedent(test), "      "),
        encoding="utf-8",
    )
    return parse_spec(str(path)).suites[0].tests[0]


class TestSpec:
    def test_hb12_expect_names_a_python_check(self, tmp_path: Path) -> None:
        test = _parsed(
            tmp_path,
            f"""\
            - trigger: "end the session"
              expect:
                check: {{python: "{CHECK}"}}
            """,
        )

        assert test.expect == Expect(check=CHECK)

    def test_hb12_a_turn_has_its_own(self, tmp_path: Path) -> None:
        test = _parsed(
            tmp_path,
            f"""\
            - turns:
                - trigger: "start a training"
                - trigger: "end the session"
                  expect: {{check: {{python: "{CHECK}"}}}}
            """,
        )

        assert test.turns is not None
        assert test.turns[1].expect == Expect(check=CHECK)

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (f'"{CHECK}"', r"check must be \{python: 'module:function'\}"),
            ('{python: "evals_checks"}', r"python must be 'module:function', got 'evals_checks'"),
            ("{python: 3}", r"python must be 'module:function'"),
            ('{python: "a:b", http: {url: "http://x"}}', r"unknown key 'http'"),
        ],
    )
    def test_hb12_anything_else_is_a_spec_error(
        self, tmp_path: Path, value: str, message: str
    ) -> None:
        with pytest.raises(SpecError, match=message):
            _parsed(tmp_path, f'- trigger: "hi"\n  expect: {{check: {value}}}\n')

    def test_hb12_it_is_part_of_what_the_test_measures(self) -> None:
        plain = Case(trigger="t", expect=SAYS_OK)
        checked = Case(trigger="t", expect=Expect(response=SAYS_OK.response, check=CHECK))

        assert checked.definition_sha256 != plain.definition_sha256
        # A test without one keeps the hash it had: results before HB-12 still join.
        assert plain.definition_sha256 == (
            "13d2833054b12753c2eafd0170299b15502d4b19778649fd7e49ddedd4fbb54a"
        )


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evals_checks.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.delitem(sys.modules, "evals_checks", raising=False)


def _hooks(*targets: str) -> SessionHooks:
    hooks = SessionHooks(
        SessionConfig(), spec_path="specs/a.highbar.yaml", timeout_ms=5_000, checks=targets
    )
    hooks.load()
    return hooks


CTX = CheckContext(
    test_id="t1",
    test_name="end the session",
    spec_path="specs/a.highbar.yaml",
    message="end the session",
    response="Done, see you!",
)


class TestHooks:
    def test_hb12_the_function_gets_the_turn_and_returns_its_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _module(
            tmp_path,
            monkeypatch,
            """\
            def no_open_trainings(ctx):
                return [f"{ctx.test_id}: {ctx.message} -> {ctx.response}"]
            """,
        )

        assert _hooks(CHECK).check(CHECK, CTX) == ["t1: end the session -> Done, see you!"]

    def test_hb12_a_function_that_cannot_be_imported_fails_at_load(self) -> None:
        with pytest.raises(SessionError, match=r"expect\.check: cannot import 'no_such_hb12'"):
            _hooks("no_such_hb12:check")

    @pytest.mark.parametrize("returned", ["None", "'cleared'", "[1]", "{'a': 1}"])
    def test_hb12_it_returns_a_list_of_messages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned: str
    ) -> None:
        """None is a function that forgot its return: unverified, never a pass."""
        _module(tmp_path, monkeypatch, f"def no_open_trainings(ctx):\n    return {returned}\n")

        with pytest.raises(SessionError, match=r"expect\.check must return a list of"):
            _hooks(CHECK).check(CHECK, CTX)

    def test_hb12_a_function_that_raises_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _module(
            tmp_path,
            monkeypatch,
            "def no_open_trainings(ctx):\n    raise ValueError('no database')\n",
        )

        with pytest.raises(SessionError, match=r"expect\.check raised ValueError: no database"):
            _hooks(CHECK).check(CHECK, CTX)


class _Checks:
    """The hooks a runner gets: no session, and a check that returns what it is told."""

    def __init__(self, result: list[str] | Exception) -> None:
        self.result = result
        self.seen: list[CheckContext] = []

    def setup(self, ctx: Any) -> dict[str, Any]:
        return {"conversation": 7}

    def teardown(self, ctx: Any, session: Any) -> None:
        pass

    def before_run(self) -> None:
        pass

    def after_run(self) -> None:
        pass

    def check(self, target: str, ctx: CheckContext) -> list[str]:
        self.seen.append(ctx)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _calls(*names: str) -> Any:
    def script(message: str, test_id: str) -> Window:
        tools = [
            Trace(
                fn=name,
                args={},
                result=None,
                started_at=1.0,
                ended_at=1.1,
                context={"test": test_id},
            )
            for name in names
        ]
        return Window(tools=tools, turns=[TurnRecord(ts=1.0, context={"test": test_id})])

    return script


CHECKED = Expect(response=SAYS_OK.response, check=CHECK)


class TestRun:
    def test_hb12_its_failures_fail_the_test(self) -> None:
        checks = _Checks(["3 trainings still open"])

        summary = _run(
            [Case(trigger="end the session", expect=CHECKED)],
            Replying(["ok, bye"], _calls("end_session")),
            session=checks,
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert "3 trainings still open" in test.errors
        (ctx,) = checks.seen
        assert (ctx.message, ctx.response, ctx.session) == (
            "end the session",
            "ok, bye",
            {"conversation": 7},
        )
        assert [tool.fn for tool in ctx.tools] == ["end_session"]
        assert len(ctx.turns) == 1
        assert ctx.test_id and ctx.test_name == "end the session"

    def test_hb12_no_failure_is_a_pass(self) -> None:
        summary = _run(
            [Case(trigger="end the session", expect=CHECKED)],
            Replying(["ok, bye"]),
            session=_Checks([]),
        )

        assert summary.tests[0].status is Status.PASSED

    def test_hb12_a_check_that_raises_leaves_the_test_unverified(self) -> None:
        summary = _run(
            [Case(trigger="end the session", expect=CHECKED)],
            Replying(["ok, bye"]),
            session=_Checks(SessionError("expect.check raised ValueError: no database")),
        )

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert "expect.check raised ValueError: no database" in test.errors

    def test_hb12_a_failure_of_highbars_own_checks_stays_a_failure(self) -> None:
        """A failure is a failure, whatever else could not be checked (combine())."""
        summary = _run(
            [Case(trigger="end the session", expect=CHECKED)],
            Replying(["no"]),
            session=_Checks(SessionError("expect.check raised ValueError: no database")),
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert "expect.check raised ValueError: no database" in test.errors

    def test_hb12_an_unverified_turn_is_not_checked(self) -> None:
        checks = _Checks([])

        summary = _run(
            [Case(trigger="end the session", expect=CHECKED)],
            Replying([OSError("connection reset")]),
            session=checks,
        )

        assert summary.tests[0].status is Status.ERROR
        assert checks.seen == []

    def test_hb12_each_turn_sees_its_own_records(self) -> None:
        checks = _Checks([])
        turns = [
            Turn(trigger="start a training", expect=Expect()),
            Turn(trigger="end the session", expect=Expect(check=CHECK)),
        ]

        def script(message: str, test_id: str) -> Window:
            name = "end_session" if message == "end the session" else "start_training"
            return _calls(name)(message, test_id)

        _run([Case(turns=turns)], Replying(["ok", "ok"], script), session=checks)

        (ctx,) = checks.seen
        assert [tool.fn for tool in ctx.tools] == ["end_session"]

    def test_hb12_a_check_needs_no_record_of_the_agent(self) -> None:
        """It reads state of its own: a test that checks only that observes nothing."""
        assert not Expect(check=CHECK).requires_observation
        assert Expect(
            check=CHECK, function_calls=[FunctionCallExpect(name="x")]
        ).requires_observation
