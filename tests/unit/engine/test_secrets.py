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
from highbar.assertions.response import ResponseAssertion, truncate
from highbar.domain.models import AgentConfig, Expect, ResponseExpect, Spec, Suite
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.schema import SpecError
from highbar.engine.secrets import redact, redact_value

TOKEN = "s3cr3t-token-value"


class TestRedact:
    def test_every_form_of_the_value_becomes_its_variable(self) -> None:
        secrets = {TOKEN: "AGENT_TOKEN"}

        assert redact(f"Bearer {TOKEN}", secrets) == "Bearer ${AGENT_TOKEN}"
        assert redact("key=ab%2Fcd%3Def", {"ab/cd=ef": "KEY"}) == "key=${KEY}"
        assert redact(repr(f"Bearer {TOKEN}\n"), {f"{TOKEN}\n": "T"}) == "'Bearer ${T}'"

    def test_short_values_are_left_alone(self) -> None:
        """A ${PORT} of 8000 masked would turn every 8000 in a message into ${PORT}."""
        assert redact("port 8000", {"8000": "PORT"}) == "port 8000"


class TestCutValues:
    """A message stops at its limit, and that limit can fall inside a value (review I5)."""

    SECRETS: dict[str, str] = {TOKEN: "AGENT_TOKEN"}  # noqa: RUF012

    @pytest.mark.parametrize("shown", range(3, len(TOKEN)))
    def test_i5_a_value_cut_anywhere_is_masked(self, shown: int) -> None:
        text = truncate("x" * 290 + TOKEN, 290 + shown)

        assert redact(text, self.SECRETS) == "x" * 290 + "${AGENT_TOKEN}…"

    def test_i5_the_quoted_form_of_a_value_is_masked_too(self) -> None:
        text = truncate("key=ab%2Fcd%3Def", 12)

        assert redact(text, {"ab/cd=ef": "KEY"}) == "key=${KEY}…"

    def test_i5_a_cut_in_a_repr_is_masked(self) -> None:
        text = repr(truncate("x" * 290 + TOKEN, 300))

        assert redact(text, self.SECRETS) == repr("x" * 290 + "${AGENT_TOKEN}…")

    def test_i5_a_value_that_repeats_its_start_is_masked_whole(self) -> None:
        assert redact("the abcabcab…", {"abcabcabc": "KEY"}) == "the ${KEY}…"

    def test_a_cut_at_two_characters_of_the_value_shows_no_mask(self) -> None:
        """Most messages cut at a letter some value starts with, and hold no value."""
        assert redact("the s3…", self.SECRETS) == "the s3…"

    def test_an_ellipsis_elsewhere_is_left_alone(self) -> None:
        assert redact("one moment… done", self.SECRETS) == "one moment… done"


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
        monkeypatch.setenv("AGENT_TOKEN", TOKEN)
        path = _spec(
            tmp_path,
            """\
            endpoint: "http://localhost:8000/chat"
            request: {headers: {Authorization: "Bearer ${AGENT_TOKEN}"}}
            """,
        )

        assert parse_spec(path).agent.secrets == {TOKEN: "AGENT_TOKEN"}

    def test_header_value_with_a_trailing_newline_is_a_spec_error_that_hides_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_TOKEN", TOKEN + "\n")
        path = _spec(
            tmp_path,
            """\
            endpoint: "http://localhost:8000/chat"
            request: {headers: {Authorization: "Bearer ${AGENT_TOKEN}"}}
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
            secrets={TOKEN: "AGENT_TOKEN"},
        ).run(
            Spec(
                agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
                suites=[
                    Suite(
                        name="S",
                        tests=[
                            Case(
                                trigger="hi",
                                expect=Expect(response=ResponseExpect(matcher={"contains": "x"})),
                            )
                        ],
                    )
                ],
            )
        )

        ((_, result),) = [c.args for c in reporter.on_test_result.call_args_list]
        assert result.errors == ["HTTP 401: echo Bearer ${AGENT_TOKEN}"]

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


class TestRedactValue:
    SECRETS: dict[str, str] = {TOKEN: "AGENT_TOKEN", "987654": "PIN"}  # noqa: RUF012

    def test_strings_keys_and_nesting(self) -> None:
        value = {TOKEN: [f"Bearer {TOKEN}", {"inner": (TOKEN,)}]}

        assert redact_value(value, self.SECRETS) == {
            "${AGENT_TOKEN}": ["Bearer ${AGENT_TOKEN}", {"inner": ("${AGENT_TOKEN}",)}]
        }

    def test_a_number_that_holds_a_secret_is_masked(self) -> None:
        assert redact_value({"pin": 987654}, self.SECRETS) == {"pin": "${PIN}"}

    def test_other_values_stay_as_they_are(self) -> None:
        value = {"a": 1, "b": 2.5, "c": True, "d": None}

        assert redact_value(value, self.SECRETS) == value
        assert redact_value(True, {"True": "FLAG"}) is True

    def test_without_secrets_the_value_is_the_same_object(self) -> None:
        value = {"a": ["b"]}

        assert redact_value(value, {}) is value
