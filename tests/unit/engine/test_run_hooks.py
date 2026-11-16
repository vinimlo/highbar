"""HB-10: ``agent.run``, a setup and a teardown that run once per spec, around the run.

The setup turns something on before the agent starts (a feature flag, a fixture); the
teardown puts it back on every way out: the end, a ceiling, an agent that does not
start, Ctrl-C or SIGTERM. A run hook belongs to no test, so it reads no placeholder.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.adapters.subprocess_adapter import AgentStartError
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    CommandHook,
    Expect,
    HttpHook,
    PythonHook,
    ResponseExpect,
    RunConfig,
    RunSummary,
    SessionConfig,
    Spec,
    Status,
    Suite,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import load_session_hooks
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.schema import SpecError
from highbar.engine.session import RunHookError, SessionError, SessionHooks
from tests.unit.engine.fakes import FakeAgent, observed

PY = sys.executable
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
ENDPOINT = "http://127.0.0.1:1/chat"


def _spec_file(tmp_path: Path, run: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  endpoint: "http://localhost:8000/chat"\n'
        + textwrap.indent(run, "  ")
        + "suites:\n  - name: S\n    tests:\n"
        + '      - trigger: "hi"\n        expect: {response: {contains: "ok"}}\n',
        encoding="utf-8",
    )
    return str(path)


class TestSpec:
    def test_hb10_a_spec_has_a_setup_and_a_teardown_for_the_whole_run(self, tmp_path: Path) -> None:
        spec = parse_spec(
            _spec_file(tmp_path, 'run:\n  setup: "flags on beta"\n  teardown: "flags off beta"\n')
        )

        assert spec.agent.run == RunConfig(
            setup=CommandHook(argv=["flags", "on", "beta"]),
            teardown=CommandHook(argv=["flags", "off", "beta"]),
        )

    def test_hb10_python_and_http_hooks_too(self, tmp_path: Path) -> None:
        spec = parse_spec(
            _spec_file(
                tmp_path,
                'run:\n  setup: {python: "flags:on"}\n'
                '  teardown: {http: {url: "http://localhost:9000/flags/beta", method: DELETE}}\n',
            )
        )

        assert spec.agent.run is not None
        assert spec.agent.run.setup == PythonHook(target="flags:on")
        assert spec.agent.run.teardown == HttpHook(
            url="http://localhost:9000/flags/beta", method="DELETE"
        )

    def test_hb10_it_needs_a_setup_or_a_teardown(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"agent\.run: needs setup or teardown"):
            parse_spec(_spec_file(tmp_path, "run: {}\n"))

    def test_hb10_an_unknown_key_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"agent\.run: unknown key 'before_run'"):
            parse_spec(_spec_file(tmp_path, 'run:\n  before_run: "flags on"\n'))

    def test_hb10_a_run_hook_has_no_test_to_read(self, tmp_path: Path) -> None:
        with pytest.raises(
            SpecError, match=r"agent\.run\.setup\[1\]: '\{\{test\.id\}\}' is not available here"
        ):
            parse_spec(_spec_file(tmp_path, 'run:\n  setup: "flags {{test.id}}"\n'))

    def test_hb10_a_value_from_the_environment_is_a_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A command hook's line is how it passes its own token on, as in agent.session."""
        monkeypatch.setenv("HB10_FLAG_TOKEN", "s3cr3t-flag")

        spec = parse_spec(
            _spec_file(tmp_path, 'run:\n  setup: "flags on --token ${HB10_FLAG_TOKEN}"\n')
        )

        assert spec.agent.run is not None
        assert spec.agent.run.setup == CommandHook(argv=["flags", "on", "--token", "s3cr3t-flag"])
        assert spec.agent.secrets == {"s3cr3t-flag": "HB10_FLAG_TOKEN"}


def _command(code: str) -> CommandHook:
    return CommandHook(argv=[PY, "-c", textwrap.dedent(code)])


def _hooks(setup: Any = None, teardown: Any = None) -> SessionHooks:
    hooks = SessionHooks(
        SessionConfig(),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=10_000,
        run=RunConfig(setup=setup, teardown=teardown),
    )
    hooks.load()
    return hooks


class TestHooks:
    def test_hb10_a_command_runs_once_for_the_run(self, tmp_path: Path) -> None:
        log = tmp_path / "flags.log"
        write = f"open({str(log)!r}, 'a').write('{{}}\\n')"
        hooks = _hooks(_command(write.format("on")), _command(write.format("off")))

        hooks.before_run()
        hooks.after_run()

        assert log.read_text(encoding="utf-8") == "on\noff\n"

    def test_hb10_a_python_hook_is_called_with_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        log = tmp_path / "flags.log"
        (tmp_path / "hb10_flags.py").write_text(
            textwrap.dedent(f"""\
                import pathlib
                LOG = pathlib.Path({str(log)!r})

                def on():
                    LOG.open("a").write("on\\n")

                def off():
                    LOG.open("a").write("off\\n")
                """),
            encoding="utf-8",
        )
        monkeypatch.delitem(sys.modules, "hb10_flags", raising=False)
        hooks = _hooks(PythonHook("hb10_flags:on"), PythonHook("hb10_flags:off"))

        hooks.before_run()
        hooks.after_run()

        assert log.read_text(encoding="utf-8") == "on\noff\n"

    def test_hb10_a_setup_that_fails_says_which_hook(self) -> None:
        hooks = _hooks(
            _command("import sys; print('flag service down', file=sys.stderr); sys.exit(1)")
        )

        with pytest.raises(RunHookError, match=r"^agent\.run\.setup exited with code 1") as info:
            hooks.before_run()

        assert "flag service down" in str(info.value)

    def test_hb10_a_setup_failure_never_shows_a_secret(self) -> None:
        """It leaves the run, past the runner that masks the errors of a test."""
        hooks = SessionHooks(
            SessionConfig(),
            spec_path="specs/a.highbar.yaml",
            timeout_ms=2_000,
            secrets={"s3cr3t-flag": "HB10_FLAG_TOKEN"},
            run=RunConfig(setup=HttpHook(url="http://127.0.0.1:1/flags?token=s3cr3t-flag")),
        )
        hooks.load()

        with pytest.raises(RunHookError) as info:
            hooks.before_run()

        assert "s3cr3t-flag" not in str(info.value)
        assert "${HB10_FLAG_TOKEN}" in str(info.value)

    def test_hb10_a_python_hook_that_cannot_be_imported_fails_at_load(self) -> None:
        spec = Spec(
            agent=AgentConfig(
                endpoint=ENDPOINT, run=RunConfig(teardown=PythonHook("no_such_module_hb10:off"))
            ),
            suites=[Suite(name="S", tests=[Case(trigger="hi", expect=SAYS_OK)])],
        )

        with pytest.raises(
            SessionError, match=r"agent\.run\.teardown.*cannot import 'no_such_module_hb10'"
        ):
            load_session_hooks("specs/a.highbar.yaml", spec)


class _RunHooks:
    """Session hooks with a run setup and teardown, writing what happens in order."""

    timeout_s = 60.0

    def __init__(
        self, events: list[str], *, fail_setup: bool = False, fail_teardown: bool = False
    ) -> None:
        self._events = events
        self._fail_setup = fail_setup
        self._fail_teardown = fail_teardown

    def before_run(self) -> None:
        self._events.append("run.setup")
        if self._fail_setup:
            raise RunHookError("agent.run.setup exited with code 1: flag service down")

    def after_run(self) -> None:
        self._events.append("run.teardown")
        if self._fail_teardown:
            raise RunHookError("agent.run.teardown exited with code 2: flag service down")

    def setup(self, ctx: Any) -> dict[str, Any]:
        return {}

    def teardown(self, ctx: Any, session: Any) -> None:
        pass


def _run(
    events: list[str],
    hooks: _RunHooks,
    *,
    adapter: MagicMock | None = None,
    reporter: MagicMock | None = None,
    script: Any = None,
) -> RunSummary:
    if adapter is None:
        adapter = MagicMock()
        adapter.start.side_effect = lambda: events.append("agent.start")
        adapter.stop.side_effect = lambda: events.append("agent.stop")

    def sent(message: str, test_id: str) -> Window:
        events.append("message")
        return observed(message, test_id) if script is None else script(message, test_id)

    agent = FakeAgent(sent)
    runner = Runner(
        adapter=adapter,
        client=agent,
        collector=agent,
        assertions=[ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        session=hooks,  # type: ignore[arg-type]
        spec_path="specs/s.highbar.yaml",
    )
    return runner.run(
        Spec(
            agent=AgentConfig(endpoint=ENDPOINT),
            suites=[Suite(name="S", tests=[Case(trigger="hi", expect=SAYS_OK)])],
        )
    )


class TestRunnerRunHooks:
    def test_hb10_setup_before_the_agent_starts_and_teardown_after_it_stops(self) -> None:
        events: list[str] = []

        _run(events, _RunHooks(events))

        assert events == ["run.setup", "agent.start", "message", "agent.stop", "run.teardown"]

    def test_hb10_a_setup_that_fails_stops_the_run_before_the_agent(self) -> None:
        events: list[str] = []

        with pytest.raises(RunHookError, match="flag service down"):
            _run(events, _RunHooks(events, fail_setup=True))

        # What the setup did not finish, the teardown does not undo.
        assert events == ["run.setup"]

    def test_hb10_teardown_runs_when_the_run_is_interrupted(self) -> None:
        events: list[str] = []

        def interrupted(message: str, test_id: str) -> Window:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _run(events, _RunHooks(events), script=interrupted)

        assert events == ["run.setup", "agent.start", "message", "agent.stop", "run.teardown"]

    def test_hb10_teardown_runs_when_stopping_the_agent_is_interrupted(self) -> None:
        """A second Ctrl-C lands while the agent is being stopped (review of #54)."""
        events: list[str] = []
        adapter = MagicMock()
        adapter.start.side_effect = lambda: events.append("agent.start")

        def interrupted() -> None:
            events.append("agent.stop")
            raise KeyboardInterrupt

        adapter.stop.side_effect = interrupted

        with pytest.raises(KeyboardInterrupt):
            _run(events, _RunHooks(events), adapter=adapter)

        assert events == ["run.setup", "agent.start", "message", "agent.stop", "run.teardown"]

    def test_hb10_teardown_runs_when_the_agent_does_not_start(self) -> None:
        events: list[str] = []
        adapter = MagicMock()

        def fails() -> None:
            events.append("agent.start")
            raise AgentStartError("port 8000 is taken")

        adapter.start.side_effect = fails

        with pytest.raises(AgentStartError):
            _run(events, _RunHooks(events), adapter=adapter)

        assert events == ["run.setup", "agent.start", "run.teardown"]

    def test_hb10_a_teardown_that_fails_is_a_warning_before_the_summary(self) -> None:
        events: list[str] = []
        reporter = MagicMock()

        summary = _run(events, _RunHooks(events, fail_teardown=True), reporter=reporter)

        assert summary.tests[0].status is Status.PASSED
        (warning,) = summary.warnings
        assert warning.startswith("agent.run.teardown exited with code 2: flag service down")
        assert "check by hand" in warning
        names = [call[0] for call in reporter.mock_calls]
        assert names.index("on_warning") < names.index("on_run_end")
