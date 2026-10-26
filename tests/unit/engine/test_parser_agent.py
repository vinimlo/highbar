"""The `agent` block in both modes: request, response, session, correlate, ${VAR} (D5)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.domain.models import CommandHook, PythonHook, RequestConfig
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError

RESPONSE_ONLY = """\
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect: {response: {contains: "ok"}}
"""

CHECKS_CALLS = """\
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect: {response: {contains: "ok"}}
      - trigger: "cancel"
        expect: {function_calls: []}
"""


def _spec(tmp_path: Path, agent: str, suites: str = RESPONSE_ONLY) -> str:
    path = tmp_path / "spec.highbar.yaml"
    body = "agent:\n" + textwrap.indent(textwrap.dedent(agent), "  ") + suites
    path.write_text(body, encoding="utf-8")
    return str(path)


MANAGED = """\
start: "python agent.py"
endpoint: "http://localhost:8000/chat"
"""

ATTACH = """\
endpoint: "http://localhost:8000/chat"
"""


class TestModes:
    def test_attach_mode_has_no_start(self, tmp_path: Path) -> None:
        agent = parse_spec(_spec(tmp_path, ATTACH)).agent

        assert agent.start is None
        assert not agent.managed

    def test_managed_mode_has_start(self, tmp_path: Path) -> None:
        assert parse_spec(_spec(tmp_path, MANAGED)).agent.managed

    def test_attach_mode_needs_traces_when_a_test_checks_calls(self, tmp_path: Path) -> None:
        with pytest.raises(
            SpecError,
            match=r"agent: attach mode \(no 'start'\) needs 'traces'.*suites\[0\]\.tests\[1\]",
        ):
            parse_spec(_spec(tmp_path, ATTACH, CHECKS_CALLS))

    def test_attach_mode_with_traces_checks_calls(self, tmp_path: Path) -> None:
        agent = parse_spec(
            _spec(tmp_path, ATTACH + 'traces: "./var/traces.jsonl"\n', CHECKS_CALLS)
        ).agent

        assert agent.traces == "./var/traces.jsonl"

    def test_attach_mode_without_traces_is_fine_for_response_only_specs(
        self, tmp_path: Path
    ) -> None:
        assert parse_spec(_spec(tmp_path, ATTACH)).agent.traces is None

    def test_traces_with_start_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="'traces' is for attach mode"):
            parse_spec(_spec(tmp_path, MANAGED + 'traces: "t.jsonl"\n'))

    def test_startup_timeout_without_start_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="startup_timeout only applies with 'start'"):
            parse_spec(_spec(tmp_path, ATTACH + "startup_timeout: 5000\n"))

    def test_endpoint_is_still_required(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="missing 'endpoint'"):
            parse_spec(_spec(tmp_path, 'start: "python agent.py"\n'))


class TestEnvironment:
    def test_env_var_is_expanded_in_agent_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_TOKEN", "abc")
        monkeypatch.setenv("AGENT_PORT", "8123")
        spec = _spec(
            tmp_path,
            """\
            endpoint: "http://localhost:${AGENT_PORT}/chat"
            request:
              headers: {Authorization: "Bearer ${AGENT_TOKEN}"}
            """,
        )

        agent = parse_spec(spec).agent

        assert agent.endpoint == "http://localhost:8123/chat"
        assert agent.request.headers == {"Authorization": "Bearer abc"}

    def test_missing_env_var_is_a_spec_error_naming_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_TOKEN", raising=False)
        spec = _spec(
            tmp_path,
            ATTACH + 'request:\n  headers: {Authorization: "Bearer ${AGENT_TOKEN}"}\n',
        )

        with pytest.raises(
            SpecError,
            match=r"agent\.request\.headers\.Authorization: environment variable "
            r"AGENT_TOKEN is not set",
        ):
            parse_spec(spec)

    def test_env_var_outside_agent_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/home/x")
        suites = RESPONSE_ONLY.replace('trigger: "hi"', 'trigger: "where is ${HOME}"')

        (test,) = parse_spec(_spec(tmp_path, ATTACH, suites)).suites[0].tests

        assert test.trigger == "where is ${HOME}"

    def test_expanded_value_is_not_expanded_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("A", "${B}")
        monkeypatch.delenv("B", raising=False)
        spec = _spec(tmp_path, ATTACH + 'request:\n  headers: {X-Value: "${A}"}\n')

        assert parse_spec(spec).agent.request.headers == {"X-Value": "${B}"}


class TestRequest:
    def test_request_defaults(self, tmp_path: Path) -> None:
        agent = parse_spec(_spec(tmp_path, ATTACH)).agent

        assert agent.request == RequestConfig()
        assert agent.request.method == "POST"
        assert agent.request.body == {"message": "{{message}}"}
        assert agent.timeout == 60000
        assert agent.correlate == {"test": "{{test.id}}"}

    def test_custom_request(self, tmp_path: Path) -> None:
        spec = _spec(
            tmp_path,
            ATTACH
            + """\
request:
  method: put
  body: {text: "{{message}}", meta: {id: "{{test.id}}"}}
""",
        )

        request = parse_spec(spec).agent.request

        assert request.method == "PUT"
        assert request.body == {"text": "{{message}}", "meta": {"id": "{{test.id}}"}}

    def test_request_body_must_send_the_message(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, ATTACH + 'request:\n  body: {text: "hello"}\n')

        with pytest.raises(SpecError, match=r"agent\.request\.body: never uses \{\{message\}\}"):
            parse_spec(spec)

    def test_unknown_placeholder_is_an_error_with_a_suggestion(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, ATTACH + 'request:\n  body: {message: "{{mesage}}"}\n')

        with pytest.raises(SpecError, match=r"unknown placeholder '\{\{mesage\}\}'.*did you mean"):
            parse_spec(spec)

    def test_previous_is_not_a_request_placeholder(self, tmp_path: Path) -> None:
        body = 'request:\n  body: {message: "{{message}}", x: "{{previous.function_calls[0]}}"}\n'

        with pytest.raises(SpecError, match="unknown placeholder"):
            parse_spec(_spec(tmp_path, ATTACH + body))

    def test_session_placeholder_needs_a_session(self, tmp_path: Path) -> None:
        body = 'request:\n  body: {message: "{{message}}", conv: "{{session.conversation_id}}"}\n'

        with pytest.raises(SpecError, match=r"\{\{session\..*\}\}' needs agent\.session"):
            parse_spec(_spec(tmp_path, ATTACH + body))

    def test_method_must_be_post_put_or_patch(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="method must be POST, PUT or PATCH"):
            parse_spec(_spec(tmp_path, ATTACH + "request:\n  method: GET\n"))

    def test_header_values_must_be_strings(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"agent\.request\.headers: X-Bot must be a string"):
            parse_spec(_spec(tmp_path, ATTACH + "request:\n  headers: {X-Bot: 1}\n"))

    def test_unknown_request_key(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="did you mean 'headers'"):
            parse_spec(_spec(tmp_path, ATTACH + "request:\n  header: {}\n"))


class TestResponseAndTimeout:
    @pytest.mark.parametrize("path", ["reply", "data.reply", "choices.0.message.content"])
    def test_response_path_syntax(self, tmp_path: Path, path: str) -> None:
        assert parse_spec(_spec(tmp_path, ATTACH + f'response: "{path}"\n')).agent.response == path

    @pytest.mark.parametrize("path", ["", "data..reply", ".reply", "data.", "data reply"])
    def test_malformed_response_path(self, tmp_path: Path, path: str) -> None:
        with pytest.raises(SpecError, match="agent: response"):
            parse_spec(_spec(tmp_path, ATTACH + f'response: "{path}"\n'))

    def test_p7_timeout_defaults_to_60000_and_must_be_positive(self, tmp_path: Path) -> None:
        assert parse_spec(_spec(tmp_path, ATTACH + "timeout: 120000\n")).agent.timeout == 120000
        with pytest.raises(SpecError, match="timeout must be a positive integer"):
            parse_spec(_spec(tmp_path, ATTACH + "timeout: 0\n"))


class TestSession:
    def test_session_command_and_python_forms(self, tmp_path: Path) -> None:
        spec = _spec(
            tmp_path,
            ATTACH
            + """\
session:
  setup: "python -m evals.session open --test '{{test.name}}'"
  teardown: {python: "evals.session:close"}
""",
        )

        session = parse_spec(spec).agent.session

        assert session is not None
        assert session.setup == CommandHook(
            argv=["python", "-m", "evals.session", "open", "--test", "{{test.name}}"]
        )
        assert session.teardown == PythonHook(target="evals.session:close")

    def test_teardown_may_use_session_placeholders(self, tmp_path: Path) -> None:
        spec = _spec(
            tmp_path,
            ATTACH + 'session:\n  setup: "open"\n  teardown: "close {{session.conversation_id}}"\n',
        )

        assert parse_spec(spec).agent.session is not None

    def test_setup_cannot_use_session_placeholders(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, ATTACH + 'session:\n  setup: "open {{session.id}}"\n')

        with pytest.raises(SpecError, match=r"agent\.session\.setup.*session"):
            parse_spec(spec)

    def test_python_hook_needs_module_and_function(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="module:function"):
            parse_spec(_spec(tmp_path, ATTACH + 'session:\n  setup: {python: "evals"}\n'))

    def test_session_needs_setup_or_teardown(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="setup or teardown"):
            parse_spec(_spec(tmp_path, ATTACH + "session: {}\n"))

    def test_unparsable_command_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="not a valid command line"):
            parse_spec(_spec(tmp_path, ATTACH + 'session:\n  setup: "open \'unclosed"\n'))


class TestCorrelate:
    def test_correlate_default_and_custom(self, tmp_path: Path) -> None:
        spec = _spec(
            tmp_path,
            ATTACH
            + """\
session: {setup: "open"}
correlate: {conversation_id: "{{session.conversation_id}}"}
""",
        )

        assert parse_spec(spec).agent.correlate == {
            "conversation_id": "{{session.conversation_id}}"
        }

    def test_correlate_must_be_a_non_empty_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="correlate must be a non-empty mapping"):
            parse_spec(_spec(tmp_path, ATTACH + "correlate: {}\n"))
