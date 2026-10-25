"""Session hooks: open a clean session before each test execution, close it after (D5).

A hook is a command or a Python function. A command runs without a shell: its
placeholders are resolved argument by argument, so a test name with a ``;`` in it is
one argument, not a second command. It runs in its own process group, killed whole
if it outlives ``agent.timeout``.

The setup's result becomes ``{{session.*}}``. A command prints it as the last line
of its output, so a setup that imports the application can log before it.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from highbar.assertions.response import truncate
from highbar.domain.models import CommandHook, PythonHook, SessionConfig
from highbar.engine.placeholders import PlaceholderError, Values, render


@dataclass(frozen=True)
class SessionContext:
    """What a hook knows about the test execution it serves."""

    test_id: str
    test_name: str
    spec_path: str


class SessionError(RuntimeError):
    """A hook failed; the message starts with ``session.setup`` or ``session.teardown``."""


class SessionHooks:
    def __init__(self, config: SessionConfig, *, spec_path: str, timeout_ms: int) -> None:
        self._config = config
        self._spec_path = spec_path
        self._timeout_s = timeout_ms / 1000
        self._functions: dict[str, Callable[..., Any]] = {}

    def load(self) -> None:
        """Import the Python hooks, once, before any agent starts. Raises ``SessionError``."""
        for what, hook in (("setup", self._config.setup), ("teardown", self._config.teardown)):
            if isinstance(hook, PythonHook):
                self._functions[what] = _load(hook.target, what)

    def setup(self, ctx: SessionContext) -> dict[str, Any]:
        """Open a session; ``{}`` when there is no setup. Raises ``SessionError``."""
        hook = self._config.setup
        if hook is None:
            return {}
        if isinstance(hook, PythonHook):
            result = self._call("setup", ctx)
            if not isinstance(result, Mapping):
                raise SessionError(
                    f"session.setup must return a mapping, got {type(result).__name__}"
                )
            session = dict(result)
        else:
            session = _last_json_object(self._run(hook, "setup", ctx, {}))
        if "headers" in session and not isinstance(session["headers"], Mapping):
            raise SessionError("session.setup returned 'headers' that is not an object")
        return session

    def teardown(self, ctx: SessionContext, session: Mapping[str, Any]) -> None:
        """Close the session. Raises ``SessionError``."""
        hook = self._config.teardown
        if hook is None:
            return
        if isinstance(hook, PythonHook):
            self._call("teardown", ctx, session)
        else:
            self._run(hook, "teardown", ctx, session)

    def _call(self, what: str, *args: Any) -> Any:
        function = self._functions[what]
        try:
            return function(*args)
        except Exception as e:
            raise SessionError(f"session.{what} raised {type(e).__name__}: {e}") from None

    def _run(
        self, hook: CommandHook, what: str, ctx: SessionContext, session: Mapping[str, Any]
    ) -> str:
        values = Values(message=None, test_id=ctx.test_id, test_name=ctx.test_name, session=session)
        try:
            argv = [_as_argument(render(token, values)) for token in hook.argv]
        except PlaceholderError as e:
            raise SessionError(f"session.{what}: {e}") from None

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as e:
            raise SessionError(
                f"session.{what}: could not run '{argv[0]}': {e.strerror or e}"
            ) from None

        try:
            stdout, stderr = process.communicate(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            raise SessionError(
                f"session.{what} did not finish in {self._timeout_s:g}s (agent.timeout)"
            ) from None
        except BaseException:
            _kill_group(process)
            raise

        if process.returncode != 0:
            tail = "\n".join(stderr.splitlines()[-10:])
            raise SessionError(
                f"session.{what} exited with code {process.returncode}"
                + (f":\n{tail}" if tail else "")
            )
        return stdout


def _load(target: str, what: str) -> Callable[..., Any]:
    module_name, _, function_name = target.partition(":")
    # Hooks live in the project highbar runs from, like `python -m` would find them.
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise SessionError(
            f"session.{what}: cannot import '{module_name}': {type(e).__name__}: {e}"
        ) from None
    function = getattr(module, function_name, None)
    if function is None:
        raise SessionError(f"session.{what}: '{module_name}' has no '{function_name}'")
    if not callable(function):
        raise SessionError(f"session.{what}: '{target}' is not a function")
    return function


def _last_json_object(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    try:
        value = json.loads(last)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise SessionError(
            "session.setup must print a JSON object as its last line of output, "
            f"got {truncate(last, 200)!r}"
        )
    return value


def _as_argument(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _kill_group(process: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate()
