"""Session hooks: open a clean session before each test execution, close it after (D5).

A hook is a command or a Python function. A command runs without a shell: its
placeholders are resolved argument by argument, so a test name with a ``;`` in it is
one argument, not a second command. It runs in its own process group, killed whole
if it outlives ``agent.timeout``.

The setup's result becomes ``{{session.*}}``. A command prints it as the last line
of its output, so a setup that imports the application can log before it.

A Python hook runs in a thread of its own, the same one that imported it, so a module
that opens a connection at import time can use it; a call that outlives
``agent.timeout`` is abandoned there, and the run goes on.

An HTTP hook is a request with placeholders in its URL, headers and body (HB-13), so
highbar can run from an image of its own, away from the agent's. A setup's reply must
be a JSON object; any reply that is not 2xx is the hook's failure.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx

from highbar._narrow import as_dict
from highbar.assertions.response import truncate
from highbar.domain.models import CommandHook, HttpHook, PythonHook, SessionConfig
from highbar.engine.placeholders import PlaceholderError, Values, render, render_url


@dataclass(frozen=True)
class SessionContext:
    """What a hook knows about the test execution it serves."""

    test_id: str
    test_name: str
    spec_path: str


class SessionError(RuntimeError):
    """A hook failed; the message starts with ``session.setup`` or ``session.teardown``."""


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
    ) -> None:
        self._config = config
        self._transport = transport
        self._spec_path = spec_path
        self._timeout_s = timeout_ms / 1000
        self._functions: dict[str, Callable[..., Any]] = {}
        self._worker: _Worker | None = None

    def load(self) -> None:
        """Import the Python hooks, once, before any agent starts. Raises ``SessionError``."""
        for what, hook in (("setup", self._config.setup), ("teardown", self._config.teardown)):
            if isinstance(hook, PythonHook):
                self._functions[what] = self._in_worker(
                    what, "import", lambda target=hook.target, what=what: _load(target, what)
                )

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
            session: dict[str, Any] = dict(cast("Mapping[str, Any]", result))
        elif isinstance(hook, HttpHook):
            session = _json_object(self._send(hook, "setup", ctx, {}))
        else:
            session = _last_json_object(self._run(hook, "setup", ctx, {}))
        if "headers" in session and not isinstance(session["headers"], Mapping):
            raise SessionError("session.setup returned 'headers' that is not an object")
        for key, value in session.items():
            # It goes into requests and commands as JSON; an ORM object would crash there.
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                raise SessionError(
                    f"session.setup returned a value that is not JSON: {key} is "
                    f"{type(value).__name__}"
                ) from None
        return session

    def teardown(self, ctx: SessionContext, session: Mapping[str, Any]) -> None:
        """Close the session. Raises ``SessionError``."""
        hook = self._config.teardown
        if hook is None:
            return
        if isinstance(hook, PythonHook):
            self._call("teardown", ctx, session)
        elif isinstance(hook, HttpHook):
            self._send(hook, "teardown", ctx, session)
        else:
            self._run(hook, "teardown", ctx, session)

    def _call(self, what: str, *args: Any) -> Any:
        function = self._functions[what]
        return self._in_worker(what, "call", lambda: function(*args))

    def _in_worker(self, what: str, action: str, task: Callable[[], Any]) -> Any:
        if self._worker is None:
            self._worker = _Worker()
        finished, outcome = self._worker.run(task, self._timeout_s)
        if not finished:
            # The thread stays stuck in the hook; the next hook gets a new one.
            self._worker = None
            raise SessionError(
                f"session.{what} did not finish in {self._timeout_s:g}s (agent.timeout)"
            )
        if "error" in outcome:
            error = outcome["error"]
            if isinstance(error, SessionError):
                raise error
            raise SessionError(f"session.{what} raised {_describe(error)}") from None
        return outcome.get("result")

    def _run(
        self, hook: CommandHook, what: str, ctx: SessionContext, session: Mapping[str, Any]
    ) -> str:
        values = Values(message=None, test_id=ctx.test_id, test_name=ctx.test_name, session=session)
        try:
            argv = [_as_argument(render(token, values)) for token in hook.argv]
        except PlaceholderError as e:
            raise SessionError(f"session.{what}: {e}") from None

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
                    f"session.{what}: could not run '{argv[0]}': {e.strerror or e}"
                ) from None

            try:
                process.wait(timeout=self._timeout_s)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                raise SessionError(
                    f"session.{what} did not finish in {self._timeout_s:g}s (agent.timeout)"
                ) from None
            except BaseException:
                _kill_group(process)
                raise
            stdout, stderr = _text(out), _text(err)

        if process.returncode != 0:
            tail = "\n".join(stderr.splitlines()[-10:])
            raise SessionError(
                f"session.{what} exited with code {process.returncode}"
                + (f":\n{tail}" if tail else "")
            )
        return stdout

    def _send(
        self, hook: HttpHook, what: str, ctx: SessionContext, session: Mapping[str, Any]
    ) -> str:
        """Send the hook's request and return the reply's text. Raises ``SessionError``."""
        values = Values(message=None, test_id=ctx.test_id, test_name=ctx.test_name, session=session)
        try:
            url = render_url(hook.url, values)
            headers = {name: _as_argument(render(v, values)) for name, v in hook.headers.items()}
            body = render(hook.body, values)
        except PlaceholderError as e:
            raise SessionError(f"session.{what}: {e}") from None
        for name, value in headers.items():
            if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                # Usually a token read with its newline, from the session or a ${VAR}.
                raise SessionError(
                    f"session.{what}: the header {name} has a control character or leading "
                    "or trailing whitespace (often a secret read with its newline); not shown"
                )
            if not value.isascii():
                # An accented test name, usually. The value is not shown either.
                raise SessionError(
                    f"session.{what}: the header {name} has characters outside ASCII, which "
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
        late = f"session.{what} did not finish in {seconds:g}s (agent.timeout)"
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
                            f"session.{what}: the reply from {url} is larger than "
                            f"{MAX_HOOK_REPLY_BYTES // 1_000_000} MB"
                        )
                    if time.monotonic() > deadline:
                        raise SessionError(late)
        except httpx.TimeoutException:
            raise SessionError(late) from None
        except httpx.ConnectError as e:
            raise SessionError(f"session.{what}: cannot reach {url}: {e}") from None
        except httpx.LocalProtocolError:
            # The library quotes the offending value whole: say which problem, never it.
            raise SessionError(
                f"session.{what}: the request to {url} could not be sent: a header value "
                "is not valid; the value is not shown"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise SessionError(f"session.{what}: {hook.method} {url} failed: {e}") from None

        text = bytes(raw).decode(reply.charset_encoding or "utf-8", errors="replace")
        status = reply.status_code
        if 300 <= status < 400:
            where = reply.headers.get("Location", "an unnamed location")
            raise SessionError(
                f"session.{what}: {hook.method} {url} returned HTTP {status}, redirected to "
                f"{where}; point the hook at that URL"
            )
        if not reply.is_success:
            raise SessionError(
                f"session.{what}: {hook.method} {url} returned HTTP {status}: {truncate(text, 200)}"
            )
        return text


class _Worker:
    """A daemon thread that runs Python hooks, imports and calls alike, one at a time."""

    def __init__(self) -> None:
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
        while True:
            task, outcome, done = self._tasks.get()
            try:
                outcome["result"] = task()
            except BaseException as e:  # SystemExit from a hook too: it is the hook's failure
                outcome["error"] = e
            done.set()


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
    mapping = as_dict(value)
    if mapping is None:
        raise SessionError(
            "session.setup must print a JSON object as its last line of output, "
            f"got {truncate(last, 200)!r}"
        )
    return mapping


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    mapping = as_dict(value)
    if mapping is None:
        raise SessionError(
            f"session.setup must return a JSON object, got {truncate(text.strip(), 200)!r}"
        )
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
