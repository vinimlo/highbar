"""A fake Braintrust REST API, and a report to send to it (HB-49).

``FakeBraintrust`` answers the three calls the export makes, as the API reference
describes them (``POST /v1/project``, ``POST /v1/experiment``,
``POST /v1/experiment/{id}/insert``), and records every request. A script of statuses
per path makes it fail: each request to that path takes the next status, and 200 once
the script is used up. A failure answers with the request's Authorization header in its
body, so a test can tell that the export never prints what the API sent back unmasked.

``write_report`` writes a report with the real ``JsonReport``: a passed, a failed and an
error test, a conversation, and a negative control.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from highbar.domain.models import (
    AgentConfig,
    Counts,
    ExecutionOutcome,
    PhrasingOutcome,
    RunSummary,
    Spec,
    Status,
    Suite,
    TestOutcome,
    TurnOutcome,
    Usage,
    UsageTotals,
)
from highbar.domain.models import Test as Case
from highbar.reporters.json_report import JsonReport

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
EXPERIMENT_ID = "22222222-2222-4222-8222-222222222222"
STARTED = "2026-11-16T12:00:00.250Z"


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class FakeBraintrust:
    url: str = ""
    requests: list[Request] = field(default_factory=list[Request])
    script: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    answered: dict[str, list[int]] = field(default_factory=dict[str, list[int]])

    def to(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path]

    @property
    def inserted(self) -> list[dict[str, Any]]:
        """Every event the fake accepted, in order."""
        return [
            event
            for request, status in zip(self.to(INSERT), self.answered.get(INSERT, []), strict=True)
            if status == 200
            for event in request.body["events"]
        ]

    def answer(self, request: Request) -> tuple[int, Any]:
        script = self.script.get(request.path, [])
        status = script.pop(0) if script else 200
        self.answered.setdefault(request.path, []).append(status)
        if status != 200:
            return status, {"error": f"boom, with {request.headers.get('authorization')}"}
        if request.path == "/v1/project":
            return 200, {"id": PROJECT_ID, "name": request.body["name"]}
        if request.path == "/v1/experiment":
            body = request.body
            return 200, {"id": EXPERIMENT_ID, "name": body["name"], "project_id": PROJECT_ID}
        if request.path == INSERT:
            return 200, {"row_ids": [event["id"] for event in request.body["events"]]}
        return 404, {"error": "no such path"}


INSERT = f"/v1/experiment/{EXPERIMENT_ID}/insert"


@contextmanager
def fake_braintrust() -> Iterator[FakeBraintrust]:
    fake = FakeBraintrust()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length)
            request = Request(
                "POST",
                self.path,
                {k.lower(): v for k, v in self.headers.items()},
                json.loads(raw) if raw else None,
            )
            with lock:
                fake.requests.append(request)
                status, body = fake.answer(request)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _execution(
    execution_id: str,
    status: Status,
    turns: tuple[TurnOutcome, ...],
    *,
    errors: tuple[str, ...] = (),
    run: int = 1,
    totals: UsageTotals | None = None,
) -> ExecutionOutcome:
    return ExecutionOutcome(execution_id, run, STARTED, status, 1500.0, errors, turns, totals)


def _test(
    suite: str,
    name: str,
    status: Status,
    phrasings: tuple[PhrasingOutcome, ...],
    *,
    expect_status: Status | None = None,
) -> TestOutcome:
    return TestOutcome(
        id=f"{suite}/{name}",
        suite=suite,
        name=name,
        definition_sha256=hashlib.sha256(f"{suite}/{name}".encode()).hexdigest(),
        status=status,
        runs=1,
        min_pass=1,
        counts=Counts(),
        duration_ms=1500.0,
        phrasings=phrasings,
        expect_status=expect_status,
    )


def sample_outcomes() -> tuple[TestOutcome, ...]:
    """passed, failed, error, a conversation, and a negative control."""
    usage = Usage(model="m", input_tokens=100, output_tokens=10, cost=0.002, currency="USD")
    shipped = TurnOutcome("Where is order #1?", "It shipped.", 900.0, {}, usage=usage)
    totals = UsageTotals(turns=1, input_tokens=100, output_tokens=10, cost={"USD": 0.002})
    passed = _execution("exec-pass", Status.PASSED, (shipped,), totals=totals)
    refund = TurnOutcome("Refund order #2", "Sure, done.", 800.0, {})
    failed = _execution(
        "exec-fail", Status.FAILED, (refund,), errors=("expected a call to refund_order",)
    )
    down = TurnOutcome("Cancel order #3", None, None, {})
    error = _execution("exec-error", Status.ERROR, (down,), errors=("HTTP 503 from /chat",))
    first = TurnOutcome("I want to return a lamp", "Which order?", 700.0, {})
    second = TurnOutcome("Order #4", "Return started.", 600.0, {})
    talk = _execution("exec-talk", Status.PASSED, (first, second))
    control = TurnOutcome("Break the tool", "Done.", 500.0, {})
    controlled = _execution("exec-control", Status.FAILED, (control,), errors=("tool boom failed",))

    def one(kind: str, text: str | None, execution: ExecutionOutcome) -> PhrasingOutcome:
        return PhrasingOutcome(kind, text, execution.status, Counts(), (execution,))

    return (
        _test("Orders", "ships", Status.PASSED, (one("trigger", shipped.trigger, passed),)),
        _test("Orders", "refund", Status.FAILED, (one("trigger", refund.trigger, failed),)),
        _test("Orders", "cancel", Status.ERROR, (one("trigger", down.trigger, error),)),
        _test("Returns", "two turns", Status.PASSED, (one("conversation", None, talk),)),
        _test(
            "Controls",
            "must fail",
            Status.PASSED,
            (one("trigger", control.trigger, controlled),),
            expect_status=Status.FAILED,
        ),
    )


def write_report(
    path: Path, *, outcomes: tuple[TestOutcome, ...] | None = None, code: int | None = 1
) -> dict[str, Any]:
    """A report as `highbar run --report json=` writes it; ``code`` None: the run died."""
    spec_path = path.parent / "shop.highbar.yaml"
    spec_path.write_text("suites: []\n", encoding="utf-8")
    spec = Spec(
        agent=AgentConfig(endpoint="http://x/chat"), suites=[Suite(name="S", tests=[Case()])]
    )
    chosen = sample_outcomes() if outcomes is None else outcomes
    summary = RunSummary(
        total=len(chosen), passed=0, failed=0, errored=0, duration_ms=1.0, tests=chosen
    )
    report = JsonReport(path, version="9.9.9")
    report.start()
    report.plan([(str(spec_path), spec)])
    report.add(str(spec_path), spec, summary, True)
    if code is not None:
        report.finish(code)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["started_at"] = STARTED  # a fixed start, so the default experiment name is known
    path.write_text(json.dumps(body), encoding="utf-8")
    return body
