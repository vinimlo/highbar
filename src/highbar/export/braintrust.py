"""A run's results as a Braintrust experiment, one event per execution (HB-49).

It reads a `--report json=` file, so `highbar run` never talks to Braintrust: a run in CI
is the same with or without an account. It speaks the REST API with httpx, as the API
reference has it (https://docs.braintrust.dev/openapi.json):

- ``POST /v1/project`` with ``{"name"}``: returns the project of that name if there is
  one, unmodified;
- ``POST /v1/experiment`` with ``{"project_id", "name"}``: the same for an experiment
  of that name in the project;
- ``POST /v1/experiment/{id}/insert`` with ``{"events": [...]}``: an event with the
  ``id`` of one already there replaces it.

So sending a report again lands in the same experiment and replaces its rows: each
event's id comes from the run and the execution's place in it.

The score is ``highbar``: 1 passed, 0 failed, and null for an error. An error was not
verified, so it is neither, and a 0 would count it as a wrong answer in every average.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from highbar._narrow import as_dict, as_list
from highbar.domain.models import Status
from highbar.engine.runs import against_expected
from highbar.engine.secrets import redact

DEFAULT_API_URL = "https://api.braintrust.dev"
DEFAULT_KEY_ENV = "BRAINTRUST_API_KEY"
SCORE = "highbar"
# The report formats this reader knows (`schema_version` changes only on a break).
SCHEMA_VERSIONS = (1,)
# Events per insert request. The API reference states no limit on a request's events,
# only 20 MB per event (https://docs.braintrust.dev/plans-and-limits.md): this is a
# guess on the safe side, and the one number to change if Braintrust says otherwise.
BATCH_SIZE = 100
# The waits before each retry of a 429, a 5xx or a request that never got an answer.
BACKOFF_S = (1.0, 2.0, 4.0)
TIMEOUT_S = 30.0
# How much of an error body goes into a message.
BODY_CHARS = 200

_SCORES: dict[Status, int | None] = {Status.PASSED: 1, Status.FAILED: 0, Status.ERROR: None}


class ReportRefused(Exception):
    """The file is not a finished highbar JSON report this version can read."""


class ApiError(Exception):
    """Braintrust did not take a request, after the retries. The key is already masked."""


class ExportFailed(Exception):
    """An export that stopped part of the way, with how far it got."""

    def __init__(self, reason: str, sent: int, total: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sent = sent
        self.total = total


def read_report(path: Path) -> dict[str, Any]:
    """The report at ``path``, or ``ReportRefused`` saying why it cannot be sent."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ReportRefused(f"cannot read {path}: {e.strerror or e}") from None
    try:
        body = as_dict(json.loads(data.decode("utf-8")))
    except ValueError:
        body = None
    if body is None or "schema_version" not in body or "exit_code" not in body:
        raise ReportRefused(
            f"{path} is not a highbar JSON report: write one with `highbar run --report json=FILE`"
        )
    version = body["schema_version"]
    if version not in SCHEMA_VERSIONS:
        known = ", ".join(str(v) for v in SCHEMA_VERSIONS)
        raise ReportRefused(
            f"{path} has schema_version {version}, and this highbar reads {known}: "
            "export it with the highbar version that wrote it"
        )
    if body["exit_code"] is None:
        raise ReportRefused(
            f"{path} is from a run that did not finish (exit_code is null): it was killed "
            "or is still going, so its results are partial; export a finished run"
        )
    return body


def experiment_name(path: Path, report: dict[str, Any]) -> str:
    """The file's stem and the run's start: a new experiment per run, the same one again."""
    return f"{path.stem} {report.get('started_at')}"


def build_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One experiment event per execution of every test. Raises ``ReportRefused``."""
    run = [report.get("started_at"), report.get("git_sha")]
    common = {
        "report_started_at": report.get("started_at"),
        "highbar_version": report.get("highbar_version"),
        "git_sha": report.get("git_sha"),
    }
    events: list[dict[str, Any]] = []
    for spec in _items(report, "specs"):
        key = str(spec.get("key") or spec.get("path"))
        for test in _items(spec, "tests"):
            expect = test.get("expect_status")
            expected = None if expect is None else _status(expect, "expect_status")
            for p, phrasing in enumerate(_items(test, "phrasings"), 1):
                for e, execution in enumerate(_items(phrasing, "executions"), 1):
                    observed = _status(execution.get("status"), "an execution's status")
                    status = against_expected(observed, expected)[0]
                    event_id = hashlib.sha256(
                        json.dumps([*run, key, test.get("id"), p, e]).encode()
                    ).hexdigest()[:32]
                    metadata: dict[str, Any] = {
                        "spec": key,
                        "test_id": test.get("id"),
                        "suite": test.get("suite"),
                        "test": test.get("name"),
                        "definition_sha256": test.get("definition_sha256"),
                        "phrasing": p,
                        "phrasing_kind": phrasing.get("kind"),
                        "run": execution.get("run"),
                        "execution_id": execution.get("execution_id"),
                        "status": str(status),
                        "observed_status": str(observed),
                        "expect_status": str(expected) if expected is not None else None,
                        "test_status": test.get("status"),
                        "errors": execution.get("errors") or [],
                        **common,
                    }
                    turns = [as_dict(t) or {} for t in as_list(execution.get("turns")) or []]
                    event_input, output = _io(phrasing, turns)
                    events.append(
                        {
                            "id": event_id,
                            "input": event_input,
                            "output": output,
                            "scores": {SCORE: _SCORES[status]},
                            "metadata": metadata,
                            "metrics": _metrics(execution, metadata),
                        }
                    )
    return events


def _items(parent: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """``parent[field]`` as a list of objects, or ``ReportRefused``."""
    items = as_list(parent.get(field))
    objects = [as_dict(item) for item in items] if items is not None else None
    if objects is None or any(item is None for item in objects):
        raise ReportRefused(f"not a highbar JSON report: `{field}` is not a list of objects")
    return [item for item in objects if item is not None]


def _status(value: object, what: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        raise ReportRefused(f"not a highbar JSON report: {what} is {value!r}") from None


def _io(phrasing: dict[str, Any], turns: list[dict[str, Any]]) -> tuple[Any, Any]:
    """The message and the reply; for a conversation, every turn's, in order."""
    if phrasing.get("kind") == "conversation":
        return [t.get("trigger") for t in turns], [t.get("response") for t in turns]
    text = phrasing.get("text")
    message = text if text is not None else (turns[0].get("trigger") if turns else None)
    return message, turns[-1].get("response") if turns else None


def _metrics(execution: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Timing, and tokens and cost when known. A partial sum is not sent as a total."""
    metrics: dict[str, Any] = {}
    start = _timestamp(execution.get("started_at"))
    duration = execution.get("duration_ms")
    if start is not None and isinstance(duration, int | float):
        metrics["start"] = start
        metrics["end"] = start + duration / 1000
    totals = as_dict(execution.get("totals"))
    if totals is None:
        return metrics
    if totals.get("complete") is not True:
        metadata["usage_warnings"] = totals.get("warnings") or []
        return metrics
    prompt, completion = totals.get("input_tokens"), totals.get("output_tokens")
    if isinstance(prompt, int):
        metrics["prompt_tokens"] = prompt
    if isinstance(completion, int):
        metrics["completion_tokens"] = completion
    if isinstance(prompt, int) and isinstance(completion, int):
        metrics["tokens"] = prompt + completion
    cost = as_dict(totals.get("cost")) or {}
    if len(cost) == 1:
        [(currency, amount)] = cost.items()
        metrics["cost"] = amount
        metadata["currency"] = currency
    elif cost:
        metadata["cost"] = cost  # two currencies do not add up to one number
    return metrics


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class Client:
    """The three calls, each retried on a 429, a 5xx or no answer at all."""

    def __init__(
        self,
        api_url: str,
        key: str,
        key_name: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._secrets = {key: key_name}
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=TIMEOUT_S,
        )

    def close(self) -> None:
        self._http.close()

    def project(self, name: str) -> str:
        return self._id(self._post("/v1/project", {"name": name}), "project")

    def experiment(self, project_id: str, name: str) -> str:
        body = {"project_id": project_id, "name": name}
        return self._id(self._post("/v1/experiment", body), "experiment")

    def insert(self, experiment_id: str, events: list[dict[str, Any]]) -> None:
        self._post(f"/v1/experiment/{experiment_id}/insert", {"events": events})

    def _id(self, body: Any, what: str) -> str:
        value = (as_dict(body) or {}).get("id")
        if not isinstance(value, str) or not value:
            raise ApiError(f"Braintrust answered the {what} without an id")
        return value

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        attempts = len(BACKOFF_S) + 1
        problem = ""
        for attempt in range(attempts):
            if attempt:
                self._sleep(BACKOFF_S[attempt - 1])
            try:
                response = self._http.post(path, json=body)
            except httpx.HTTPError as e:
                problem = f"no answer to POST {path} ({type(e).__name__}: {e})"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                problem = self._refused(path, response)
                continue
            if response.is_success:
                try:
                    return response.json()
                except ValueError:
                    return None
            raise ApiError(self._mask(self._refused(path, response)))
        raise ApiError(self._mask(f"{problem}, after {attempts} attempts"))

    def _refused(self, path: str, response: httpx.Response) -> str:
        text = " ".join(response.text.split())
        if len(text) > BODY_CHARS:
            text = text[:BODY_CHARS] + "…"
        return f"HTTP {response.status_code} from POST {path}: {text}"

    def _mask(self, text: str) -> str:
        return redact(text, self._secrets)


def export(events: list[dict[str, Any]], client: Client, *, project: str, experiment: str) -> None:
    """Send ``events`` into the experiment. Raises ``ExportFailed`` with the count sent."""
    sent = 0
    try:
        project_id = client.project(project)
        experiment_id = client.experiment(project_id, experiment)
        for start in range(0, len(events), BATCH_SIZE):
            batch = events[start : start + BATCH_SIZE]
            client.insert(experiment_id, batch)
            sent += len(batch)
    except ApiError as e:
        raise ExportFailed(str(e), sent, len(events)) from None
