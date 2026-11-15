"""HB-22: what a Python hook prints or logs goes to a file, not into the run's output.

A setup that imports the whole application logs at INFO from its first line, and those
lines landed in the middle of highbar's output. They go to ``.highbar/logs/``, next to
the agent's log, and the path and the last lines show only when the hook fails.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest

from highbar.domain.models import AgentConfig, PythonHook, SessionConfig, Spec
from highbar.engine.run_spec import load_session_hooks
from highbar.engine.session import SessionContext, SessionError, SessionHooks

CTX = SessionContext(test_id="abc", test_name="cancel order", spec_path="specs/a.highbar.yaml")
TOKEN = "s3cr3t-token-value"

# An application that sets up its own logging when it is imported, as a Flask app does.
APP = """\
import logging, os, sys, time

log = logging.getLogger("app")
log.handlers.clear()
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False
log.info("app imported")


def setup(ctx):
    print("setup printed")
    log.info("setup logged")
    sys.stderr.write("setup wrote to stderr\\n")
    return {"ok": 1}


def quiet(ctx):
    return {}


def broken(ctx):
    for n in range(1, 16):
        print(f"line {n}")
    raise RuntimeError("db is down")


def leaks(ctx):
    print("connecting with " + os.environ["HB22_TOKEN"])
    raise RuntimeError("refused")


def slow(ctx):
    print("about to hang")
    time.sleep(5)
    return {}
"""


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    # Each test imports its own copy, from its own folder.
    for name in ("hb22_app", "hb22_quiet", "hb22_bad", "hb22_root", "hb22_wraps"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    (tmp_path / "hb22_app.py").write_text(APP, encoding="utf-8")
    return tmp_path


def _hooks(target: str, *, timeout_ms: int = 10_000) -> SessionHooks:
    hooks = SessionHooks(
        SessionConfig(setup=PythonHook(f"hb22_app:{target}")),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=timeout_ms,
    )
    hooks.load()
    return hooks


def _logs(root: Path) -> list[Path]:
    return sorted((root / ".highbar" / "logs").glob("a-hooks-*.log"))


def test_hb22_what_a_python_hook_prints_or_logs_goes_to_a_file(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _hooks("setup").setup(CTX) == {"ok": 1}

    shown = "".join(capsys.readouterr())
    for line in ("app imported", "setup printed", "setup logged", "setup wrote to stderr"):
        assert line not in shown
    (log,) = _logs(app)
    written = log.read_text(encoding="utf-8")
    for line in ("INFO app: app imported", "setup printed", "INFO app: setup logged"):
        assert line in written
    assert "setup wrote to stderr" in written


def test_hb22_the_runs_own_output_still_reaches_the_terminal(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = _hooks("setup")
    print("before the hook")
    hooks.setup(CTX)
    print("after the hook")

    out = capsys.readouterr().out
    assert "before the hook" in out
    assert "after the hook" in out


def test_hb22_a_hook_that_writes_nothing_leaves_no_file(app: Path) -> None:
    (app / "hb22_quiet.py").write_text("def setup(ctx):\n    return {}\n", encoding="utf-8")
    hooks = SessionHooks(
        SessionConfig(setup=PythonHook("hb22_quiet:setup")),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=10_000,
    )
    hooks.load()

    hooks.setup(CTX)

    assert not (app / ".highbar").exists()


def test_hb22_a_failing_hook_names_the_file_and_shows_its_last_lines(app: Path) -> None:
    hooks = _hooks("broken")

    with pytest.raises(SessionError) as info:
        hooks.setup(CTX)

    message = str(info.value)
    (log,) = _logs(app)
    assert message.startswith("session.setup raised RuntimeError: db is down")
    assert f".highbar/logs/{log.name}" in message
    assert "line 6\n" in message
    assert message.endswith("line 15")
    assert "line 5\n" not in message


def test_hb22_a_hook_that_times_out_shows_what_it_printed(app: Path) -> None:
    hooks = _hooks("slow", timeout_ms=500)

    with pytest.raises(SessionError) as info:
        hooks.setup(CTX)

    assert str(info.value).startswith("session.setup did not finish in 0.5s")
    assert str(info.value).endswith("about to hang")


def test_hb22_a_hook_that_fails_at_import_shows_what_it_printed(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (app / "hb22_bad.py").write_text(
        textwrap.dedent(
            """\
            print("loading settings")
            raise ImportError("no module named 'settings_local'")
            def setup(ctx):
                return {}
            """
        ),
        encoding="utf-8",
    )
    hooks = SessionHooks(
        SessionConfig(setup=PythonHook("hb22_bad:setup")),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=10_000,
    )

    with pytest.raises(SessionError) as info:
        hooks.load()

    assert "loading settings" not in "".join(capsys.readouterr())
    assert str(info.value).endswith("loading settings")


def test_hb22_the_lines_shown_mask_the_secrets(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HB22_TOKEN", TOKEN)
    spec = Spec(
        agent=AgentConfig(
            endpoint="http://127.0.0.1:1/chat",
            session=SessionConfig(setup=PythonHook("hb22_app:leaks")),
            secrets={TOKEN: "AGENT_TOKEN"},
        ),
        suites=[],
    )
    hooks = load_session_hooks("specs/a.highbar.yaml", spec)
    assert hooks is not None

    with pytest.raises(SessionError) as info:
        hooks.setup(CTX)

    assert TOKEN not in str(info.value)
    assert str(info.value).endswith("connecting with ${AGENT_TOKEN}")


def test_hb22_a_handler_the_hook_set_up_writes_to_the_file_from_any_thread(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``logging.basicConfig`` in the application puts a handler on the root logger, and
    the run's own HTTP client logs through it: those lines are the hook's doing too."""
    (app / "hb22_root.py").write_text(
        textwrap.dedent(
            """\
            import logging
            root_handler = logging.StreamHandler()
            logging.getLogger().addHandler(root_handler)

            def setup(ctx):
                return {}
            """
        ),
        encoding="utf-8",
    )
    hooks = SessionHooks(
        SessionConfig(setup=PythonHook("hb22_root:setup")),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=10_000,
    )
    hooks.load()
    try:
        hooks.setup(CTX)
        logging.getLogger("hb22.client").warning("HTTP Request: POST /chat")
    finally:
        logging.getLogger().removeHandler(sys.modules["hb22_root"].root_handler)

    assert "HTTP Request" not in "".join(capsys.readouterr())
    (log,) = _logs(app)
    assert "HTTP Request: POST /chat" in log.read_text(encoding="utf-8")


def test_hb22_a_module_that_wraps_stdout_at_import_leaves_the_run_its_terminal(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The review of 4fa3e97: colorama.init() wraps sys.stdout when it is not a terminal,
    as in CI, and the wrapper kept the run's own lines going to the hook's file."""
    (app / "hb22_wraps.py").write_text(
        textwrap.dedent(
            """\
            import sys

            class Wrapper:
                def __init__(self, wrapped):
                    self.wrapped = wrapped
                def write(self, text):
                    return self.wrapped.write(text)
                def flush(self):
                    self.wrapped.flush()

            sys.stdout = Wrapper(sys.stdout)

            def setup(ctx):
                return {}
            """
        ),
        encoding="utf-8",
    )
    hooks = SessionHooks(
        SessionConfig(setup=PythonHook("hb22_wraps:setup")),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=10_000,
    )
    hooks.load()
    hooks.setup(CTX)

    print("✓ where is my order")

    assert "✓ where is my order" in capsys.readouterr().out


def test_hb22_the_hook_log_is_kept_out_of_git(app: Path) -> None:
    """In attach mode no agent writes the folder's .gitignore, and the file holds raw values."""
    _hooks("setup").setup(CTX)

    assert (app / ".highbar" / "logs" / ".gitignore").read_text(encoding="utf-8").strip() == "*"
