"""Values that came from ${VAR} never reach the output (PR 5 review, I2).

A token in a header, an API key in the endpoint's query: CI output is public enough.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from highbar.adapters.http_client import AgentResponseError
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import AgentConfig, Expect, ResponseExpect, Spec, Suite
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.schema import SpecError
from highbar.engine.secrets import redact

TOKEN = "s3cr3t-token-value"


class TestRedact:
    def test_every_form_of_the_value_becomes_its_variable(self) -> None:
        secrets = {TOKEN: "SAIA_EVAL_TOKEN"}

        assert redact(f"Bearer {TOKEN}", secrets) == "Bearer ${SAIA_EVAL_TOKEN}"
        assert redact("key=ab%2Fcd%3Def", {"ab/cd=ef": "KEY"}) == "key=${KEY}"
        assert redact(repr(f"Bearer {TOKEN}\n"), {f"{TOKEN}\n": "T"}) == "'Bearer ${T}'"

    def test_short_values_are_left_alone(self) -> None:
        """A ${PORT} of 8000 masked would turn every 8000 in a message into ${PORT}."""
        assert redact("port 8000", {"8000": "PORT"}) == "port 8000"


def _spec(tmp_path: Path, agent: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        "agent:\n"
        + textwrap.indent(textwrap.dedent(agent), "  ")
        + "suites:\n  - name: S\n    tests:\n"
        + "      - {trigger: hi, expect: {response: {contains: ok}}}\n",
        encoding="utf-8",
    )
    return str(path)


class TestParser:
    def test_expanded_values_are_kept_to_be_masked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAIA_EVAL_TOKEN", TOKEN)
        path = _spec(
            tmp_path,
            """\
            endpoint: "http://localhost:8000/chat"
            request: {headers: {Authorization: "Bearer ${SAIA_EVAL_TOKEN}"}}
            """,
        )

        assert parse_spec(path).agent.secrets == {TOKEN: "SAIA_EVAL_TOKEN"}

    def test_header_value_with_a_trailing_newline_is_a_spec_error_that_hides_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAIA_EVAL_TOKEN", TOKEN + "\n")
        path = _spec(
            tmp_path,
            """\
            endpoint: "http://localhost:8000/chat"
            request: {headers: {Authorization: "Bearer ${SAIA_EVAL_TOKEN}"}}
            """,
        )

        with pytest.raises(SpecError) as info:
            parse_spec(path)

        assert "agent.request.headers.Authorization" in str(info.value)
        assert "whitespace" in str(info.value)
        assert "s3cr3t" not in str(info.value)

    def test_a_spec_error_never_shows_an_expanded_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_URL", f"localhost:8000/chat?key={TOKEN}")
        path = _spec(tmp_path, 'endpoint: "${AGENT_URL}"\n')

        with pytest.raises(SpecError) as info:
            parse_spec(path)

        assert "s3cr3t" not in str(info.value)
        assert "${AGENT_URL}" in str(info.value)

    @pytest.mark.parametrize("key", ["endpoint", "health"])
    def test_i5_placeholders_in_the_urls_are_rejected(self, tmp_path: Path, key: str) -> None:
        url = "http://localhost:8000/conversations/{{session.cid}}/messages"
        agent = f'endpoint: "{url}"\n' if key == "endpoint" else (
            f'endpoint: "http://localhost:8000/chat"\nhealth: "{url}"\n'
        )  # fmt: skip

        with pytest.raises(SpecError, match=rf"agent: {key} .*placeholders are not resolved"):
            parse_spec(_spec(tmp_path, agent + 'session: {setup: "echo {}"}\n'))


class TestOutput:
    def test_test_errors_and_warnings_are_masked(self) -> None:
        client = MagicMock()
        client.send.side_effect = AgentResponseError(f"HTTP 401: echo Bearer {TOKEN}")
        reporter = MagicMock()

        Runner(
            adapter=MagicMock(),
            client=client,
            collector=MagicMock(),
            assertions=[ResponseAssertion()],
            reporter=reporter,
            settle_time=0,
            secrets={TOKEN: "SAIA_EVAL_TOKEN"},
        ).run(
            Spec(
                agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
                suites=[
                    Suite(
                        name="S",
                        tests=[
                            Case(
                                trigger="hi",
                                expect=Expect(response=ResponseExpect(contains="x")),
                            )
                        ],
                    )
                ],
            )
        )

        ((_, result),) = [c.args for c in reporter.on_test_result.call_args_list]
        assert result.errors == ["HTTP 401: echo Bearer ${SAIA_EVAL_TOKEN}"]

    def test_attach_mode_start_failure_never_shows_the_key(self, tmp_path: Path) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        (tmp_path / "spec.highbar.yaml").write_text(
            f"""\
agent:
  endpoint: "http://127.0.0.1:{port}/chat"
  health: "http://127.0.0.1:{port}/health?key=${{HB_TEST_KEY}}"
suites:
  - name: S
    tests:
      - {{trigger: hi, expect: {{response: {{contains: ok}}}}}}
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "run", "spec.highbar.yaml"],
            cwd=tmp_path,
            env={**os.environ, "HB_TEST_KEY": TOKEN, "COLUMNS": "200"},
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert "s3cr3t" not in result.stdout + result.stderr
        assert "key=${HB_TEST_KEY}" in result.stdout
