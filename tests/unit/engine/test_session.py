"""Session hooks: a clean state per test execution (spec D5; finding D11)."""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    CommandHook,
    Expect,
    PythonHook,
    ResponseExpect,
    SessionConfig,
    Spec,
    Status,
    Suite,
    Turn,
)
from highbar.domain.models import Test as Case
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.session import SessionContext, SessionError, SessionHooks
from tests.unit.engine.fakes import FakeAgent

PY = sys.executable
CTX = SessionContext(test_id="abc", test_name="cancel order", spec_path="specs/a.highbar.yaml")


def _command(code: str, *extra: str) -> CommandHook:
    return CommandHook(argv=[PY, "-c", textwrap.dedent(code), *extra])


def _hooks(setup: Any = None, teardown: Any = None, *, timeout_ms: int = 10_000) -> SessionHooks:
    hooks = SessionHooks(
        SessionConfig(setup=setup, teardown=teardown),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=timeout_ms,
    )
    hooks.load()
    return hooks


class TestCommandHooks:
    def test_command_setup_returns_its_json_object(self) -> None:
        hooks = _hooks(_command("import json; print(json.dumps({'cid': 7}))"))

        assert hooks.setup(CTX) == {"cid": 7}

    def test_setup_logs_before_the_json_are_ignored(self) -> None:
        """A setup that imports the app prints its logs first; the last line is the object."""
        hooks = _hooks(
            _command(
                """\
                import json
                print("INFO connecting to the database")
                print("INFO table 3 is free")
                print(json.dumps({"cid": 9}))
                print()
                """
            )
        )

        assert hooks.setup(CTX) == {"cid": 9}

    def test_setup_output_that_is_not_an_object_is_an_error(self) -> None:
        hooks = _hooks(_command("print('[1, 2]')"))

        with pytest.raises(SessionError, match="must print a JSON object"):
            hooks.setup(CTX)

    def test_setup_nonzero_exit_shows_the_stderr_tail(self) -> None:
        hooks = _hooks(_command("import sys; sys.stderr.write('db is down\\n'); sys.exit(3)"))

        with pytest.raises(SessionError, match=r"exited with code 3[\s\S]*db is down"):
            hooks.setup(CTX)

    def test_setup_that_hangs_times_out(self) -> None:
        hooks = _hooks(_command("import time; time.sleep(30)"), timeout_ms=500)
        started = time.monotonic()

        with pytest.raises(SessionError, match=r"did not finish in 0\.5s"):
            hooks.setup(CTX)

        assert time.monotonic() - started < 3

    def test_unknown_command_is_an_error(self) -> None:
        hooks = _hooks(CommandHook(argv=["definitely-not-a-highbar-hook"]))

        with pytest.raises(SessionError, match="could not run 'definitely-not-a-highbar-hook'"):
            hooks.setup(CTX)

    def test_placeholders_are_rendered_per_argument_not_through_a_shell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        hooks = _hooks(
            _command(
                "import json, sys; print(json.dumps({'args': sys.argv[1:]}))",
                "{{test.name}}",
                "id={{test.id}}",
            )
        )
        hostile = SessionContext(test_id="abc", test_name="a; touch PWNED", spec_path="s")

        assert hooks.setup(hostile) == {"args": ["a; touch PWNED", "id=abc"]}
        assert not (tmp_path / "PWNED").exists()

    def test_teardown_gets_the_session_values(self, tmp_path: Path) -> None:
        out = tmp_path / "closed.txt"
        hooks = _hooks(
            teardown=_command(
                "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                str(out),
                "{{session.cid}}",
            )
        )

        hooks.teardown(CTX, {"cid": 7})

        assert out.read_text() == "7"

    def test_no_setup_is_an_empty_session(self) -> None:
        assert _hooks(teardown=_command("pass")).setup(CTX) == {}


class TestPythonHooks:
    def _module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        log = tmp_path / "calls.log"
        (tmp_path / "evals_session_hooks.py").write_text(
            textwrap.dedent(f"""\
                import pathlib
                LOG = pathlib.Path({str(log)!r})
                with LOG.open("a") as f:
                    f.write("import\\n")

                def open_session(ctx):
                    with LOG.open("a") as f:
                        f.write(f"setup {{ctx.test_id}} {{ctx.test_name}} {{ctx.spec_path}}\\n")
                    return {{"cid": 11, "headers": {{"Authorization": "Bearer t"}}}}

                def close_session(ctx, session):
                    with LOG.open("a") as f:
                        f.write(f"teardown {{session['cid']}}\\n")

                def broken(ctx):
                    raise ValueError("no free table")

                def not_a_mapping(ctx):
                    return [1]
                """),
            encoding="utf-8",
        )
        monkeypatch.delitem(sys.modules, "evals_session_hooks", raising=False)
        return log

    def test_python_hooks_are_loaded_once_and_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = self._module(tmp_path, monkeypatch)
        hooks = _hooks(
            PythonHook("evals_session_hooks:open_session"),
            PythonHook("evals_session_hooks:close_session"),
        )

        session = hooks.setup(CTX)
        hooks.teardown(CTX, session)
        hooks.setup(CTX)

        assert session == {"cid": 11, "headers": {"Authorization": "Bearer t"}}
        assert log.read_text().splitlines() == [
            "import",
            "setup abc cancel order specs/a.highbar.yaml",
            "teardown 11",
            "setup abc cancel order specs/a.highbar.yaml",
        ]

    def test_python_hook_that_cannot_be_imported_fails_at_load(self) -> None:
        hooks = SessionHooks(
            SessionConfig(setup=PythonHook("no_such_module_for_highbar:open")),
            spec_path="s",
            timeout_ms=1000,
        )

        with pytest.raises(SessionError, match="cannot import 'no_such_module_for_highbar'"):
            hooks.load()

    def test_python_hook_without_the_function_fails_at_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._module(tmp_path, monkeypatch)
        hooks = SessionHooks(
            SessionConfig(setup=PythonHook("evals_session_hooks:missing")),
            spec_path="s",
            timeout_ms=1000,
        )

        with pytest.raises(SessionError, match="has no 'missing'"):
            hooks.load()

    def test_python_setup_exception_is_a_session_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._module(tmp_path, monkeypatch)
        hooks = _hooks(PythonHook("evals_session_hooks:broken"))

        with pytest.raises(SessionError, match="raised ValueError: no free table"):
            hooks.setup(CTX)

    def test_python_setup_must_return_a_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._module(tmp_path, monkeypatch)
        hooks = _hooks(PythonHook("evals_session_hooks:not_a_mapping"))

        with pytest.raises(SessionError, match="must return a mapping"):
            hooks.setup(CTX)


class _FakeSession:
    def __init__(self, *, fail_setup: bool = False, fail_teardown: bool = False) -> None:
        self.setups: list[SessionContext] = []
        self.teardowns: list[tuple[SessionContext, dict[str, Any]]] = []
        self._fail_setup = fail_setup
        self._fail_teardown = fail_teardown

    def setup(self, ctx: SessionContext) -> dict[str, Any]:
        if self._fail_setup:
            raise SessionError("session.setup exited with code 1: no free table")
        self.setups.append(ctx)
        return {"cid": len(self.setups)}

    def teardown(self, ctx: SessionContext, session: dict[str, Any]) -> None:
        self.teardowns.append((ctx, session))
        if self._fail_teardown:
            raise SessionError("session.teardown exited with code 2")


def _run(
    tests: list[Case], session: _FakeSession, *, variations: Any = None
) -> tuple[FakeAgent, MagicMock]:
    agent, reporter = FakeAgent(), MagicMock()
    runner = Runner(
        adapter=MagicMock(),
        client=agent,
        collector=agent,
        assertions=[ResponseAssertion()],
        reporter=reporter,
        settle_time=0,
        variations=variations,
        session=session,  # type: ignore[arg-type]
        spec_path="specs/s.highbar.yaml",
    )
    runner.run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=tests)],
        )
    )
    return agent, reporter


SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
SAYS_NOPE = Expect(response=ResponseExpect(matcher={"contains": "nope"}))


class TestRunnerSessions:
    def test_d11_turns_of_a_multi_turn_test_share_one_session(self) -> None:
        session = _FakeSession()
        test = Case(turns=[Turn(trigger=t, expect=SAYS_OK) for t in ("one", "two", "three")])

        agent, _ = _run([test], session)

        assert len(session.setups) == 1
        assert [values.session for values in agent.values] == [{"cid": 1}] * 3
        assert len(session.teardowns) == 1

    def test_each_test_gets_its_own_session(self) -> None:
        session = _FakeSession()

        agent, _ = _run(
            [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)], session
        )

        assert [values.session for values in agent.values] == [{"cid": 1}, {"cid": 2}]
        assert [ctx.test_id for ctx in session.setups] == agent.test_ids
        assert session.setups[0].test_name == "a"
        assert session.setups[0].spec_path == "specs/s.highbar.yaml"

    def test_each_variation_gets_its_own_session(self) -> None:
        session = _FakeSession()
        generator = MagicMock()
        generator.generate.return_value = ["b", "c"]

        _run([Case(trigger="a", expect=SAYS_OK, variations=2)], session, variations=generator)

        assert len(session.setups) == 3
        assert len(session.teardowns) == 3

    def test_failed_setup_is_an_error_and_skips_teardown(self) -> None:
        session = _FakeSession(fail_setup=True)

        agent, reporter = _run([Case(trigger="a", expect=SAYS_OK)], session)

        (_, result) = reporter.on_test_result.call_args[0]
        assert result.status is Status.ERROR
        assert "no free table" in result.errors[0]
        assert agent.sent == []
        assert session.teardowns == []

    def test_teardown_runs_after_a_failure_and_its_failure_is_only_a_warning(self) -> None:
        session = _FakeSession(fail_teardown=True)

        _, reporter = _run([Case(trigger="a", expect=SAYS_NOPE)], session)

        (_, result) = reporter.on_test_result.call_args[0]
        assert result.status is Status.FAILED
        assert len(session.teardowns) == 1
        reporter.on_warning.assert_called_once()
        assert "session teardown failed" in reporter.on_warning.call_args[0][0]


class TestReviewFindings:
    """PR 5 review: hooks that hang, crash the run, or return what cannot be sent."""

    def test_i3_python_setup_that_hangs_stops_at_agent_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hangs_hook.py").write_text(
            "import time\ndef setup(ctx):\n    time.sleep(5)\n    return {}\n", encoding="utf-8"
        )
        hooks = _hooks(setup=PythonHook("hangs_hook:setup"), timeout_ms=500)
        started = time.monotonic()

        with pytest.raises(SessionError, match=r"session.setup did not finish in 0.5s"):
            hooks.setup(CTX)

        assert time.monotonic() - started < 2

    def test_python_hooks_run_in_the_thread_that_imported_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that opens SQLite at import can use it in its hooks."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "db_hook.py").write_text(
            textwrap.dedent("""\
                import sqlite3
                db = sqlite3.connect(":memory:")
                def setup(ctx):
                    return {"one": db.execute("select 1").fetchone()[0]}
            """),
            encoding="utf-8",
        )

        assert _hooks(setup=PythonHook("db_hook:setup")).setup(CTX) == {"one": 1}

    def test_i4_sys_exit_in_a_python_hook_is_a_session_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exits_hook.py").write_text(
            "import sys\ndef setup(ctx):\n    sys.exit('database is down')\n", encoding="utf-8"
        )

        with pytest.raises(SessionError, match="SystemExit: database is down"):
            _hooks(setup=PythonHook("exits_hook:setup")).setup(CTX)

    def test_i4_setup_value_that_is_not_json_is_a_session_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "uuid_hook.py").write_text(
            "import uuid\ndef setup(ctx):\n    return {'conversation_id': uuid.uuid4()}\n",
            encoding="utf-8",
        )

        with pytest.raises(SessionError, match="not JSON: conversation_id is UUID"):
            _hooks(setup=PythonHook("uuid_hook:setup")).setup(CTX)

    def test_i4_non_utf8_hook_output_is_not_a_crash(self) -> None:
        setup = _command("""\
            import sys
            sys.stderr.buffer.write(b"conex\\xe3o recusada\\n")
            print('{"cid": 1}')
        """)

        assert _hooks(setup=setup).setup(CTX) == {"cid": 1}

        failing = _command("""\
            import sys
            sys.stderr.buffer.write(b"conex\\xe3o recusada\\n")
            sys.exit(3)
        """)
        with pytest.raises(SessionError, match="exited with code 3"):
            _hooks(setup=failing).setup(CTX)

    def test_m2_setup_that_leaves_a_daemon_holding_its_output_still_returns(
        self, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "daemon.pid"
        setup = _command(f"""\
            import subprocess, sys
            daemon = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
            )
            open({str(pid_file)!r}, "w").write(str(daemon.pid))
            print('{{"cid": 1}}')
        """)
        started = time.monotonic()
        try:
            assert _hooks(setup=setup, timeout_ms=5000).setup(CTX) == {"cid": 1}
            assert time.monotonic() - started < 4
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_teardown_that_raises_anything_is_a_warning(self) -> None:
        session = _FakeSession()
        session.teardown = MagicMock(side_effect=TypeError("boom"))  # type: ignore[method-assign]

        _, reporter = _run([Case(trigger="a", expect=SAYS_OK)], session)

        ((_, result),) = [c.args for c in reporter.on_test_result.call_args_list]
        assert result.status is Status.PASSED
        assert "session teardown failed: TypeError: boom" in reporter.on_warning.call_args[0][0]
