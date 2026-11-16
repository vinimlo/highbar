"""`highbar export braintrust` against a fake Braintrust API (HB-49).

An error is not a failure: it is sent with a null score, never a 0, or a run where the
agent was down would read in Braintrust as an agent that got everything wrong.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from highbar.cli.main import app
from highbar.domain.models import Status
from tests.fixtures.braintrust import (
    EXPERIMENT_ID,
    INSERT,
    PROJECT_ID,
    STARTED,
    FakeBraintrust,
    fake_braintrust,
    sample_outcomes,
    write_report,
)

KEY = "bt-sk-0123456789abcdefSECRET"
runner = CliRunner()


@pytest.fixture
def fake() -> Any:
    with fake_braintrust() as api:
        yield api


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRAINTRUST_API_KEY", KEY)
    # No real sleeps between retries in a test.
    monkeypatch.setattr("highbar.export.braintrust.BACKOFF_S", (0.0, 0.0, 0.0))
    path = tmp_path / "nightly.json"
    write_report(path)
    return path


def _export(report: Path, fake: FakeBraintrust, *args: str) -> Result:
    return runner.invoke(
        app,
        ["export", "braintrust", str(report), "--project", "shop-agent", "--api-url", fake.url]
        + list(args),
    )


def _nowhere() -> str:
    """A local URL where nothing listens: a port the system just gave and took back."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def _by_test(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {event["metadata"]["test_id"]: event for event in events}


class TestSending:
    def test_scores_are_1_0_and_null(self, report: Path, fake: FakeBraintrust) -> None:
        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        events = _by_test(fake.inserted)
        assert events["Orders/ships"]["scores"] == {"highbar": 1}
        assert events["Orders/refund"]["scores"] == {"highbar": 0}
        # An error could not be verified: null, which Braintrust leaves out of its
        # averages, and never 0.
        assert events["Orders/cancel"]["scores"] == {"highbar": None}
        assert "5 events" in result.output

    def test_the_project_and_experiment_carry_the_names(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        result = _export(report, fake, "--experiment", "pr-42")

        assert result.exit_code == 0, result.output
        assert [r.body for r in fake.to("/v1/project")] == [{"name": "shop-agent"}]
        experiment = fake.to("/v1/experiment")[0].body
        assert experiment["project_id"] == PROJECT_ID
        assert experiment["name"] == "pr-42"
        assert len(fake.to(f"/v1/experiment/{EXPERIMENT_ID}/insert")) == 1

    def test_the_experiment_defaults_to_the_file_and_its_start(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        assert fake.to("/v1/experiment")[0].body["name"] == f"nightly {STARTED}"

    def test_every_call_carries_the_bearer_key(self, report: Path, fake: FakeBraintrust) -> None:
        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        assert fake.requests
        assert {r.headers["authorization"] for r in fake.requests} == {f"Bearer {KEY}"}

    def test_api_key_env_names_another_variable(
        self, report: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAINTRUST_API_KEY")
        monkeypatch.setenv("CI_BT_KEY", "other-key-987654")

        result = _export(report, fake, "--api-key-env", "CI_BT_KEY")

        assert result.exit_code == 0, result.output
        assert fake.requests[0].headers["authorization"] == "Bearer other-key-987654"

    def test_events_are_sent_in_batches(
        self, report: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("highbar.export.braintrust.BATCH_SIZE", 2)

        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        assert [len(r.body["events"]) for r in fake.to(INSERT)] == [2, 2, 1]


class TestContent:
    def test_a_trigger_is_the_input_and_the_reply_the_output(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        _export(report, fake)

        event = _by_test(fake.inserted)["Orders/ships"]
        assert event["input"] == "Where is order #1?"
        assert event["output"] == "It shipped."
        meta = event["metadata"]
        assert meta["suite"] == "Orders"
        assert meta["test"] == "ships"
        assert meta["status"] == "passed"
        assert len(meta["definition_sha256"]) == 64
        assert meta["phrasing"] == 1
        assert meta["phrasing_kind"] == "trigger"
        assert meta["run"] == 1
        assert meta["execution_id"] == "exec-pass"

    def test_a_conversation_sends_every_turn(self, report: Path, fake: FakeBraintrust) -> None:
        _export(report, fake)

        event = _by_test(fake.inserted)["Returns/two turns"]
        assert event["input"] == ["I want to return a lamp", "Order #4"]
        assert event["output"] == ["Which order?", "Return started."]

    def test_no_reply_is_a_null_output(self, report: Path, fake: FakeBraintrust) -> None:
        _export(report, fake)

        event = _by_test(fake.inserted)["Orders/cancel"]
        assert event["output"] is None
        assert event["metadata"]["status"] == "error"
        assert event["metadata"]["errors"] == ["HTTP 503 from /chat"]

    def test_a_negative_control_that_failed_as_expected_scores_1(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        _export(report, fake)

        event = _by_test(fake.inserted)["Controls/must fail"]
        assert event["scores"] == {"highbar": 1}
        meta = event["metadata"]
        assert meta["status"] == "passed"
        assert meta["observed_status"] == "failed"
        assert meta["expect_status"] == "failed"

    def test_a_negative_control_that_passed_scores_0_and_says_why(
        self, tmp_path: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BRAINTRUST_API_KEY", KEY)
        control = sample_outcomes()[-1]
        execution = replace(control.phrasings[0].executions[0], status=Status.PASSED, errors=())
        phrasing = replace(control.phrasings[0], executions=(execution,))
        path = tmp_path / "xpass.json"
        write_report(path, outcomes=(replace(control, phrasings=(phrasing,)),))

        result = _export(path, fake)

        assert result.exit_code == 0, result.output
        [event] = fake.inserted
        assert event["scores"] == {"highbar": 0}
        assert event["metadata"]["status"] == "failed"
        assert "negative control no longer catches" in event["metadata"]["errors"][0]

    def test_tokens_and_cost_are_metrics_when_known(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        _export(report, fake)

        events = _by_test(fake.inserted)
        metrics = events["Orders/ships"]["metrics"]
        assert metrics["prompt_tokens"] == 100
        assert metrics["completion_tokens"] == 10
        assert metrics["tokens"] == 110
        assert metrics["cost"] == pytest.approx(0.002)
        assert events["Orders/ships"]["metadata"]["currency"] == "USD"
        assert metrics["end"] - metrics["start"] == pytest.approx(1.5)
        # Unknown is not zero: a turn that reported no usage sends no token metric.
        assert "prompt_tokens" not in events["Orders/refund"]["metrics"]
        assert "cost" not in events["Orders/refund"]["metrics"]

    def test_ids_are_stable_so_sending_again_replaces(
        self, report: Path, fake: FakeBraintrust
    ) -> None:
        _export(report, fake)
        _export(report, fake)

        first, second = (r.body["events"] for r in fake.to(INSERT))
        assert [e["id"] for e in first] == [e["id"] for e in second]
        assert len({e["id"] for e in first}) == len(first)
        # The same experiment name too: Braintrust returns the existing one.
        names = {r.body["name"] for r in fake.to("/v1/experiment")}
        assert len(names) == 1


class TestRefusals:
    def test_a_run_that_died_is_exit_2(
        self, tmp_path: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BRAINTRUST_API_KEY", KEY)
        path = tmp_path / "died.json"
        write_report(path, code=None)

        result = _export(path, fake)

        assert result.exit_code == 2, result.output
        assert "did not finish" in result.output
        assert fake.requests == []

    def test_a_file_that_is_not_a_report_is_exit_2(
        self, tmp_path: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAINTRUST_API_KEY", KEY)
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"tests": []}), encoding="utf-8")

        result = _export(path, fake)

        assert result.exit_code == 2, result.output
        assert "not a highbar JSON report" in result.output
        assert fake.requests == []

    def test_a_file_that_is_not_json_is_exit_2(
        self, tmp_path: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAINTRUST_API_KEY", KEY)
        path = tmp_path / "junit.xml"
        path.write_text("<testsuites/>", encoding="utf-8")

        result = _export(path, fake)

        assert result.exit_code == 2, result.output
        assert "not a highbar JSON report" in result.output

    def test_a_missing_file_is_exit_2(self, tmp_path: Path, fake: FakeBraintrust) -> None:
        result = _export(tmp_path / "nope.json", fake)

        assert result.exit_code == 2, result.output
        assert "nope.json" in result.output

    def test_an_unknown_schema_version_is_exit_2(self, report: Path, fake: FakeBraintrust) -> None:
        body = json.loads(report.read_text(encoding="utf-8"))
        body["schema_version"] = 2
        report.write_text(json.dumps(body), encoding="utf-8")

        result = _export(report, fake)

        assert result.exit_code == 2, result.output
        assert "schema_version 2" in result.output
        assert fake.requests == []

    def test_a_missing_key_is_exit_2_and_names_the_variable(
        self, report: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAINTRUST_API_KEY")
        monkeypatch.setenv("SOME_OTHER", KEY)

        result = _export(report, fake)

        assert result.exit_code == 2, result.output
        assert "BRAINTRUST_API_KEY" in result.output
        assert KEY not in result.output
        assert fake.requests == []

    def test_an_empty_key_is_missing_too(
        self, report: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI_BT_KEY", "")

        result = _export(report, fake, "--api-key-env", "CI_BT_KEY")

        assert result.exit_code == 2, result.output
        assert "CI_BT_KEY" in result.output
        assert fake.requests == []


class TestFailures:
    def test_a_500_is_retried(self, report: Path, fake: FakeBraintrust) -> None:
        fake.script[INSERT] = [500]

        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        assert len(fake.to(INSERT)) == 2
        assert len(fake.inserted) == 5

    def test_a_429_is_retried(self, report: Path, fake: FakeBraintrust) -> None:
        fake.script["/v1/project"] = [429, 503]

        result = _export(report, fake)

        assert result.exit_code == 0, result.output
        assert len(fake.to("/v1/project")) == 3

    def test_a_persistent_500_is_exit_1_with_the_counts(
        self, report: Path, fake: FakeBraintrust, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("highbar.export.braintrust.BATCH_SIZE", 2)
        fake.script[INSERT] = [200] + [500] * 10

        result = _export(report, fake)

        assert result.exit_code == 1, result.output
        assert "2 of 5 events sent, 3 not sent" in result.output
        assert "HTTP 500" in result.output
        assert len(fake.to(INSERT)) == 1 + 4  # the first batch, then 4 attempts

    def test_a_400_is_not_retried(self, report: Path, fake: FakeBraintrust) -> None:
        fake.script["/v1/experiment"] = [400]

        result = _export(report, fake)

        assert result.exit_code == 1, result.output
        assert len(fake.to("/v1/experiment")) == 1
        assert "0 of 5 events sent, 5 not sent" in result.output

    def test_an_api_that_is_not_there_is_exit_1(self, report: Path) -> None:
        result = runner.invoke(
            app,
            ["export", "braintrust", str(report), "--project", "p", "--api-url", _nowhere()],
        )

        assert result.exit_code == 1, result.output
        assert "0 of 5 events sent" in result.output


class TestSecrets:
    @pytest.mark.parametrize(
        "script",
        [
            {},
            {INSERT: [500] * 10},
            {"/v1/project": [401]},
            {"/v1/experiment": [403]},
        ],
        ids=["ok", "500", "401", "403"],
    )
    def test_the_key_is_never_shown(
        self,
        report: Path,
        fake: FakeBraintrust,
        caplog: pytest.LogCaptureFixture,
        script: dict[str, list[int]],
    ) -> None:
        fake.script.update({path: list(statuses) for path, statuses in script.items()})
        caplog.set_level(logging.DEBUG)

        result = _export(report, fake)

        assert fake.requests  # the key was sent, and the API echoed it on failures
        assert KEY not in result.output
        assert KEY not in result.stderr
        assert KEY not in caplog.text
        if script:
            assert "${BRAINTRUST_API_KEY}" in result.output

    def test_the_key_is_not_shown_when_the_api_is_not_there(
        self, report: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        result = runner.invoke(
            app,
            ["export", "braintrust", str(report), "--project", "p", "--api-url", _nowhere()],
        )

        assert result.exit_code == 1, result.output
        assert KEY not in result.output
        assert KEY not in result.stderr
        assert KEY not in caplog.text
