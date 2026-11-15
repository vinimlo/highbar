"""Session hooks: open a clean session before each test execution, close it after (D5).

A hook is a command or a Python function. A command runs without a shell: its
placeholders are resolved argument by argument, so a test name with a ``;`` in it is
one argument, not a second command. It runs in its own process group, killed whole
if it outlives ``agent.timeout``.

The setup's result becomes ``{{session.*}}``. A command prints it as the last line
of its output, so a setup that imports the application can log before it.

A Python hook runs in a thread of its own, the same one that imported it, so a module
that opens a connection at import time can use it; a call that outlives
``agent.timeout`` is abandoned there, and the run goes on. What that thread prints, and
what the logging handlers its module made at import write, goes to
``.highbar/logs/<spec>-hooks-<timestamp>.log``, not into the run's output (HB-22); a
hook's failure names the file and ends with its last lines.

An HTTP hook is a request with placeholders in its URL, headers and body (HB-13), so
highbar can run from an image of its own, away from the agent's. A setup's reply must
be a JSON object; any reply that is not 2xx is the hook's failure.

``agent.run`` has the same three kinds of hook, run once per spec (HB-10): its setup
before the agent starts, its teardown on every way out of a run whose setup finished.
They belong to no test, so they read no placeholder, and a Python one is called with
no argument.

An ``expect.check`` is a Python function too (HB-12), imported and called in the same
thread: it gets a ``CheckContext`` of the turn and returns its failures.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

import httpx

from highbar._narrow import as_dict
from highbar.assertions.response import truncate
from highbar.domain.models import (
    CommandHook,
    HttpHook,
    PythonHook,
    RunConfig,
    SessionConfig,
    Trace,
    TurnRecord,
)
from highbar.engine.logs import log_path, make_folder
from highbar.engine.placeholders import PlaceholderError, Values, render, render_url
from highbar.engine.secrets import redact


@dataclass(frozen=True)
class SessionContext:
    """What a hook knows about the test execution it serves."""

    test_id: str
    test_name: str
    spec_path: str
    # The test's own session.args (HB-9): the state this execution starts from.
    args: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # The test's history (HB-24): the conversation the setup seeds before its message.
    history: Sequence[Mapping[str, str]] = ()
    # What agent.run.setup returned (HB-10), the same for every test: {{run.*}}.
    run: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class CheckContext:
    """What an ``expect.check`` function gets of the turn it checks (HB-12).

    ``tools`` and ``turns`` are the records highbar counted as this turn's, as its own
    checks read them; a record written after the turn's window is not in them. Of a turn
    highbar did not observe, they are ``Unobserved``: reading them is an error.
    """

    test_id: str
    test_name: str
    spec_path: str
    message: str  # the trigger as sent, its templates resolved
    response: str
    session: Mapping[str, Any] = field(default_factory=dict[str, Any])
    args: Mapping[str, Any] = field(default_factory=dict[str, Any])
    history: Sequence[Mapping[str, str]] = ()
    tools: Sequence[Trace] = ()
    turns: Sequence[TurnRecord] = ()


class SessionError(RuntimeError):
    """A hook failed; the message starts with the hook: ``session.setup``, for one."""


class Unobserved(Sequence[Any]):
    """``ctx.tools`` or ``ctx.turns`` of a turn highbar has no record of.

    An empty list would say the agent called nothing, and a guardrail of the author's
    would pass against an agent nobody saw (review of #55). Reading it raises instead,
    and ``read`` stays set when the check catches that, so the turn is still unverified.
    """

    def __init__(self, name: str, why: str) -> None:
        why = why.removeprefix("not observed: ")
        self.message = f"expect.check read ctx.{name} of a turn highbar did not observe: {why}"
        self.read = False

    def _refuse(self) -> NoReturn:
        self.read = True
        raise SessionError(self.message)

    def __len__(self) -> NoReturn:
        self._refuse()

    def __getitem__(self, index: Any) -> NoReturn:
        self._refuse()

    def __iter__(self) -> NoReturn:
        self._refuse()

    def __contains__(self, value: object) -> NoReturn:
        self._refuse()

    def __bool__(self) -> NoReturn:
        self._refuse()

    def __eq__(self, other: object) -> NoReturn:
        self._refuse()

    __hash__ = None  # pyright: ignore[reportAssignmentType]

    def __repr__(self) -> str:
        return "<not observed>"


class RunHookError(SessionError):
    """``agent.run.setup`` or ``agent.run.teardown`` failed (HB-10)."""


# A setup's reply becomes the session, which goes into every request: past this, it is
# not a session object.
MAX_HOOK_REPLY_BYTES = 10_000_000


class SessionHooks:
    def __init__(
        self,
        config: SessionConfig,
        *,
        spec_path: str,
        timeout_ms: int,
        transport: httpx.BaseTransport | None = None,
        secrets: Mapping[str, str] | None = None,
        run: RunConfig | None = None,
        checks: Sequence[str] = (),
    ) -> None:
        self._config = config
        self._run_config = run
        self._check_targets = tuple(checks)
        self._checks: dict[str, Callable[..., Any]] = {}
        self._transport = transport
        self._spec_path = spec_path
        self._timeout_s = timeout_ms / 1000
        self._functions: dict[str, Callable[..., Any]] = {}
        self._worker: _Worker | None = None
        # The lines a failure shows go to the terminal: masked like any message.
        self._secrets = dict(secrets or {})
        self._log = _HookLog(spec_path)
        # What agent.run.setup returned, for its teardown's {{run.*}}.
        self._run_values: dict[str, Any] = {}

    def load(self) -> None:
        """Import the Python hooks, once, before any agent starts. Raises ``SessionError``."""
        hooks = [("session.setup", self._config.setup), ("session.teardown", self._config.teardown)]
        if self._run_config is not None:
            hooks += [
                ("agent.run.setup", self._run_config.setup),
                ("agent.run.teardown", self._run_config.teardown),
            ]
        for target in self._check_targets:
            self._checks[target] = self._in_worker(
                "expect.check", "import", lambda target=target: _load(target, "expect.check")
            )
        for what, hook in hooks:
            if isinstance(hook, PythonHook):
                self._functions[what] = self._in_worker(
                    what, "import", lambda target=hook.target, what=what: _load(target, what)
                )

    def setup(self, ctx: SessionContext) -> dict[str, Any]:
        """Open a session; ``{}`` when there is no setup. Raises ``SessionError``."""
        hook = self._config.setup
        if hook is None:
            return {}
        session = self._result("session.setup", hook, ctx)
        if "headers" in session and not isinstance(session["headers"], Mapping):
            raise SessionError("session.setup returned 'headers' that is not an object")
        return session

    def teardown(self, ctx: SessionContext, session: Mapping[str, Any]) -> None:
        """Close the session. Raises ``SessionError``."""
        hook = self._config.teardown
        if hook is None:
            return
        if isinstance(hook, PythonHook):
            self._call("session.teardown", ctx, session)
        elif isinstance(hook, HttpHook):
            self._send(hook, "session.teardown", ctx, session)
        else:
            self._run(hook, "session.teardown", ctx, session)

    def before_run(self) -> dict[str, Any]:
        """``agent.run.setup``, once, before the agent starts: what it returned when the
        spec reads it as ``{{run.*}}``, else ``{}``. Raises ``RunHookError``."""
        config = self._run_config
        if config is None or config.setup is None:
            return {}
        try:
            if config.read_result:
                self._run_values = self._result("agent.run.setup", config.setup, None)
            else:
                self._run_hook("agent.run.setup", config.setup)
        except SessionError as e:
            # It leaves the run, past the runner that masks a test's errors.
            raise RunHookError(redact(str(e), self._secrets)) from None
        return dict(self._run_values)

    def after_run(self) -> None:
        """``agent.run.teardown``, once the run is over. Raises ``RunHookError``."""
        hook = self._run_config.teardown if self._run_config else None
        if hook is None:
            return
        try:
            self._run_hook("agent.run.teardown", hook)
        except SessionError as e:
            raise RunHookError(redact(str(e), self._secrets)) from None

    def _run_hook(self, what: str, hook: CommandHook | PythonHook | HttpHook) -> None:
        # A run hook reads no test's placeholder (the parser refuses them), only {{run.*}}.
        no_test = SessionContext(
            test_id="", test_name="", spec_path=self._spec_path, run=self._run_values
        )
        if isinstance(hook, PythonHook):
            self._call(what)
        elif isinstance(hook, HttpHook):
            self._send(hook, what, no_test, {})
        else:
            self._run(hook, what, no_test, {})

    def _result(
        self, what: str, hook: CommandHook | PythonHook | HttpHook, ctx: SessionContext | None
    ) -> dict[str, Any]:
        """What a setup returned: the JSON object a session's or a run's values come from.

        ``ctx`` is the test's; a run's setup has none, and its Python function gets nothing.
        """
        values_ctx = ctx or SessionContext(test_id="", test_name="", spec_path=self._spec_path)
        if isinstance(hook, PythonHook):
            result = self._call(what, ctx) if ctx is not None else self._call(what)
            if not isinstance(result, Mapping):
                raise SessionError(f"{what} must return a mapping, got {type(result).__name__}")
            values: dict[str, Any] = dict(cast("Mapping[str, Any]", result))
        elif isinstance(hook, HttpHook):
            values = _json_object(self._send(hook, what, values_ctx, {}), what)
        else:
            values = _last_json_object(self._run(hook, what, values_ctx, {}), what)
        for key, value in values.items():
            # It goes into requests and commands as JSON; an ORM object would crash there.
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                raise SessionError(
                    f"{what} returned a value that is not JSON: {key} is {type(value).__name__}"
                ) from None
        return values

    def check(self, target: str, ctx: CheckContext) -> list[str]:
        """The failures ``expect.check`` found; none is a pass. Raises ``SessionError``.

        Anything but a list of messages (``None`` from a function that forgot its
        ``return`` included) is an error, never a pass.
        """
        function = self._checks[target]
        found = self._in_worker("expect.check", "call", lambda: function(ctx))
        messages = cast("list[Any]", found) if isinstance(found, (list, tuple)) else None
        if messages is None or not all(isinstance(m, str) for m in messages):
            raise SessionError(
                "expect.check must return a list of failure messages (empty when the turn "
                f"passes), got {_shape(found)}"
            )
        return [str(message) for message in messages]

    def _call(self, what: str, *args: Any) -> Any:
        function = self._functions[what]
        return self._in_worker(what, "call", lambda: function(*args))

    def _in_worker(self, what: str, action: str, task: Callable[[], Any]) -> Any:
        if self._worker is None:
            self._worker = _Worker(self._log)
        mark = self._log.mark()
        with _routed(self._log):
            finished, outcome = self._worker.run(task, self._timeout_s)
        if not finished:
            # The thread stays stuck in the hook; the next hook gets a new one.
            self._worker = None
            raise SessionError(
                self._with_output(
                    f"{what} did not finish in {self._timeout_s:g}s (agent.timeout)",
                    mark,
                )
            )
        if "error" in outcome:
            error = outcome["error"]
            message = (
                str(error)
                if isinstance(error, SessionError)
                else f"{what} raised {_describe(error)}"
            )
            raise SessionError(self._with_output(message, mark)) from None
        return outcome.get("result")

    def _with_output(self, message: str, mark: int) -> str:
        """``message``, and when the hook wrote anything in this call, where and its end."""
        lines = [line for line in self._log.since(mark).splitlines() if line.strip()]
        if not lines:
            return message
        tail = "\n".join(lines[-TAIL_LINES:])
        shown = f"{message}; its output is in {self._log.path}, ending with:\n{tail}"
        return redact(shown, self._secrets)

    def _run(
        self, hook: CommandHook, what: str, ctx: SessionContext, session: Mapping[str, Any]
    ) -> str:
        values = Values(
            message=None,
            test_id=ctx.test_id,
            test_name=ctx.test_name,
            session=session,
            args=ctx.args,
            history=ctx.history,
            run=ctx.run,
        )
        try:
            argv = [_as_argument(render(token, values)) for token in hook.argv]
        except PlaceholderError as e:
            raise SessionError(f"{what}: {e}") from None

        # Output goes to files, not pipes: a daemon the hook leaves behind may hold a
        # pipe open forever, and waiting for its end would hang the run.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as e:
                raise SessionError(
                    f"{what}: could not run '{argv[0]}': {e.strerror or e}"
                ) from None

            try:
                process.wait(timeout=self._timeout_s)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                raise SessionError(
                    f"{what} did not finish in {self._timeout_s:g}s (agent.timeout)"
                ) from None
            except BaseException:
                _kill_group(process)
                raise
            stdout, stderr = _text(out), _text(err)

        if process.returncode != 0:
            tail = "\n".join(stderr.splitlines()[-10:])
            raise SessionError(
                f"{what} exited with code {process.returncode}" + (f":\n{tail}" if tail else "")
            )
        return stdout

    def _send(
        self, hook: HttpHook, what: str, ctx: SessionContext, session: Mapping[str, Any]
    ) -> str:
        """Send the hook's request and return the reply's text. Raises ``SessionError``."""
        values = Values(
            message=None,
            test_id=ctx.test_id,
            test_name=ctx.test_name,
            session=session,
            args=ctx.args,
            history=ctx.history,
            run=ctx.run,
        )
        try:
            url = render_url(hook.url, values)
            headers = {name: _as_argument(render(v, values)) for name, v in hook.headers.items()}
            body = render(hook.body, values)
        except PlaceholderError as e:
            raise SessionError(f"{what}: {e}") from None
        for name, value in headers.items():
            if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                # Usually a token read with its newline, from the session or a ${VAR}.
                raise SessionError(
                    f"{what}: the header {name} has a control character or leading "
                    "or trailing whitespace (often a secret read with its newline); not shown"
                )
            if not value.isascii():
                # An accented test name, usually. The value is not shown either.
                raise SessionError(
                    f"{what}: the header {name} has characters outside ASCII, which "
                    "a header cannot carry; send the value in the body or the URL instead"
                )

        content: dict[str, Any] = {}
        if isinstance(body, str):
            content["content"] = body.encode("utf-8")
            if not any(name.lower() == "content-type" for name in headers):
                headers["Content-Type"] = "text/plain; charset=utf-8"
        elif body is not None:
            content["json"] = body

        seconds = self._timeout_s
        late = f"{what} did not finish in {seconds:g}s (agent.timeout)"
        # httpx's timeout bounds each read, not the whole reply: the deadline bounds it.
        deadline = time.monotonic() + seconds
        try:
            with (
                httpx.Client(timeout=seconds, transport=self._transport) as client,
                client.stream(hook.method, url, headers=headers, **content) as reply,
            ):
                raw = bytearray()
                for chunk in reply.iter_bytes():
                    raw += chunk
                    if len(raw) > MAX_HOOK_REPLY_BYTES:
                        raise SessionError(
                            f"{what}: the reply from {url} is larger than "
                            f"{MAX_HOOK_REPLY_BYTES // 1_000_000} MB"
                        )
                    if time.monotonic() > deadline:
                        raise SessionError(late)
        except httpx.TimeoutException:
            raise SessionError(late) from None
        except httpx.ConnectError as e:
            raise SessionError(f"{what}: cannot reach {url}: {e}") from None
        except httpx.LocalProtocolError:
            # The library quotes the offending value whole: say which problem, never it.
            raise SessionError(
                f"{what}: the request to {url} could not be sent: a header value "
                "is not valid; the value is not shown"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise SessionError(f"{what}: {hook.method} {url} failed: {e}") from None

        text = bytes(raw).decode(reply.charset_encoding or "utf-8", errors="replace")
        status = reply.status_code
        if 300 <= status < 400:
            where = reply.headers.get("Location", "an unnamed location")
            raise SessionError(
                f"{what}: {hook.method} {url} returned HTTP {status}, redirected to "
                f"{where}; point the hook at that URL"
            )
        if not reply.is_success:
            raise SessionError(
                f"{what}: {hook.method} {url} returned HTTP {status}: {truncate(text, 200)}"
            )
        return text


# How many of a failed hook's last lines its message shows.
TAIL_LINES = 10

# Set in a hook's thread: where what it writes goes.
_HOOK_THREAD = threading.local()


class _HookLog:
    """The file a spec's Python hooks write to, made at their first write (HB-22)."""

    def __init__(self, spec_path: str) -> None:
        self._spec_path = spec_path
        self.path: Path | None = None
        self._written = 0
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            if self.path is None:
                self.path = log_path(self._spec_path, "hooks")
                make_folder(self.path)
            # Opened for each write: a hook abandoned at its timeout may write at any time.
            with self.path.open("a", encoding="utf-8", errors="replace") as file:
                file.write(text)
            self._written += len(text)
        return len(text)

    def mark(self) -> int:
        """Where the next write starts, to tell what one call wrote."""
        with self._lock:
            return self._written

    def since(self, mark: int) -> str:
        with self._lock:
            if self.path is None:
                return ""
            return self.path.read_text(encoding="utf-8", errors="replace")[mark:]


class _Routed:
    """``sys.stdout`` or ``sys.stderr`` while a Python hook runs: what a hook's thread
    writes goes to its log, and the rest goes on unchanged.

    Once the call is over, only what the hook kept writes here: a logging handler its
    module made at import, as ``logging.basicConfig`` does. That handler also catches
    the run's own logging, an HTTP client's included, so all it writes goes to the log.
    """

    def __init__(self, original: TextIO, log: _HookLog) -> None:
        self._original = original
        self._log = log
        self.installed = True

    def _target(self) -> _HookLog | None:
        log: _HookLog | None = getattr(_HOOK_THREAD, "log", None)
        return log if log is not None or self.installed else self._log

    def write(self, text: str) -> int:
        log = self._target()
        return self._original.write(text) if log is None else log.write(text)

    def writelines(self, lines: Iterator[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        if self._target() is None:
            self._original.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


@contextlib.contextmanager
def _routed(log: _HookLog) -> Iterator[None]:
    """Route the hook's thread's output while the run waits for it, then put it back."""
    saved = sys.stdout, sys.stderr
    routed = _Routed(saved[0], log), _Routed(saved[1], log)
    sys.stdout, sys.stderr = cast("TextIO", routed[0]), cast("TextIO", routed[1])
    try:
        yield
    finally:
        for stream in routed:
            stream.installed = False
        # The run's own streams, even over a wrapper the hook put on ours (colorama.init()
        # does when the output is not a terminal, as in CI): what follows is the run's.
        sys.stdout, sys.stderr = saved


class _Worker:
    """A daemon thread that runs Python hooks, imports and calls alike, one at a time."""

    def __init__(self, log: _HookLog) -> None:
        self._log = log
        self._tasks: queue.Queue[tuple[Callable[[], Any], dict[str, Any], threading.Event]] = (
            queue.Queue()
        )
        threading.Thread(target=self._serve, name="highbar-session", daemon=True).start()

    def run(self, task: Callable[[], Any], timeout: float) -> tuple[bool, dict[str, Any]]:
        outcome: dict[str, Any] = {}
        done = threading.Event()
        self._tasks.put((task, outcome, done))
        return done.wait(timeout), outcome

    def _serve(self) -> None:
        _HOOK_THREAD.log = self._log
        while True:
            task, outcome, done = self._tasks.get()
            try:
                outcome["result"] = task()
            except BaseException as e:  # SystemExit from a hook too: it is the hook's failure
                outcome["error"] = e
            done.set()


def _shape(value: Any) -> str:
    """What a check returned, by kind: its values may be the agent's data."""
    kind = type(value).__name__
    if isinstance(value, (list, tuple)):
        items = cast("list[Any]", value)
        return f"a {kind} of {', '.join(sorted({type(item).__name__ for item in items}))}"
    return kind


def _describe(error: BaseException) -> str:
    try:
        message = str(error)
    except Exception:
        message = ""
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _text(file: Any) -> str:
    file.seek(0)
    return file.read().decode("utf-8", errors="replace")


def _load(target: str, what: str) -> Callable[..., Any]:
    module_name, _, function_name = target.partition(":")
    # Hooks live in the project highbar runs from, like `python -m` would find them.
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise SessionError(
            f"{what}: cannot import '{module_name}': {type(e).__name__}: {e}"
        ) from None
    function = getattr(module, function_name, None)
    if function is None:
        raise SessionError(f"{what}: '{module_name}' has no '{function_name}'")
    if not callable(function):
        raise SessionError(f"{what}: '{target}' is not a function")
    return function


def _last_json_object(stdout: str, what: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    try:
        value = json.loads(last)
    except ValueError:
        value = None
    mapping = as_dict(value)
    if mapping is None:
        raise SessionError(
            f"{what} must print a JSON object as its last line of output, "
            f"got {truncate(last, 200)!r}"
        )
    return mapping


def _json_object(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    mapping = as_dict(value)
    if mapping is None:
        raise SessionError(f"{what} must return a JSON object, got {truncate(text.strip(), 200)!r}")
    return mapping


def _as_argument(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise PlaceholderError(f"a {type(value).__name__} cannot be a command argument") from None


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
