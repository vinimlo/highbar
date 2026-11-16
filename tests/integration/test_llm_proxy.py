"""HB-30 end to end: the real CLI, the real proxy, and an agent whose own model client
reads OPENAI_BASE_URL, in front of a fake upstream."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.adapters.test_llm_proxy import (
    CHAT_REPLY,
    SSE_HEADERS,
    Answer,
    FakeUpstream,
    _chat_chunk,
    _json,
    _sse,
)

AGENT = Path(__file__).parent / "fixtures" / "proxied_agent.py"
KEY = "sk-proj-0123456789-very-secret"
SYSTEM = "internal policy: refunds over 500 need a manager"

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    fake = FakeUpstream()
    yield fake
    fake.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spec(tmp_path: Path, *, proxy: bool, budget: str = "{max_input_tokens: 500}") -> Path:
    port = _free_port()
    spec = tmp_path / "orders.highbar.yaml"
    spec.write_text(
        f"""\
agent:
  start: "{sys.executable} {AGENT} {port}"
  endpoint: "http://127.0.0.1:{port}/chat"
  settle_time: 200
  llm_proxy: {"true" if proxy else "false"}
suites:
  - name: Orders
    tests:
      - name: where is my order
        trigger: "where is my order 1042"
        expect:
          response: {{contains: "transit"}}
          budget: {budget}
""",
        encoding="utf-8",
    )
    return spec


def _run(
    tmp_path: Path, spec: Path, upstream: FakeUpstream, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "highbar.cli.main",
            "run",
            str(spec),
            "--report",
            f"json={report}",
            *args,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "COLUMNS": "200",
            "OPENAI_BASE_URL": f"{upstream.url}/v1",
            "OPENAI_API_KEY": KEY,
            "PROXIED_SYSTEM": SYSTEM,
            "PROXIED_TURN_USAGE": json.dumps(
                {"model": "gpt-4o-mini", "input_tokens": 10, "output_tokens": 2}
            ),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=110,
    )
    return result, json.loads(report.read_text(encoding="utf-8"))


def _test(report: dict[str, Any]) -> dict[str, Any]:
    (spec,) = report["specs"]
    (test,) = spec["tests"]
    return test


REPLY = {
    **CHAT_REPLY,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "in transit"}}],
}


def test_hb30_the_budget_fails_on_what_the_model_was_sent_not_on_what_the_turn_says(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    # The turn says 10 input tokens; the upstream says the model read 1000.
    upstream.answers["/v1/chat/completions"] = Answer(_json(REPLY))

    without, report_without = _run(tmp_path, _spec(tmp_path, proxy=False), upstream)
    with_proxy, report_with = _run(tmp_path, _spec(tmp_path, proxy=True), upstream)

    assert without.returncode == 0, without.stdout + without.stderr
    assert _test(report_without)["status"] == "passed"
    assert with_proxy.returncode == 1, with_proxy.stdout + with_proxy.stderr
    test = _test(report_with)
    assert test["status"] == "failed"
    assert "used 1000 input tokens" in " ".join(test["errors"])
    # Through the proxy, not around it: the upstream saw both runs' calls.
    assert len(upstream.received) == 2


def test_hb30_a_stream_without_usage_leaves_the_budget_error(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    stream = _sse(_chat_chunk("in "), _chat_chunk("transit"), "[DONE]")
    # A server that ignores stream_options: no chunk ever says the usage.
    upstream.answers["/v1/chat/completions"] = Answer(headers=SSE_HEADERS, events=[stream])

    result, report = _run(tmp_path, _spec(tmp_path, proxy=True), upstream, PROXIED_STREAM="1")

    assert result.returncode == 1, result.stdout + result.stderr
    test = _test(report)
    assert test["status"] == "error", test["errors"]
    assert "stream ended without usage" in " ".join(test["errors"])


def test_hb30_the_agent_gets_the_stream_it_would_get_without_the_proxy(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    # The agent's loop reads choices[0] of every chunk: the usage chunk would break it.
    upstream.answers["/v1/chat/completions"] = Answer(
        headers=SSE_HEADERS,
        events=[_sse(_chat_chunk("in "), _chat_chunk("transit"), "[DONE]")],
        usage_events=[
            _sse(
                _chat_chunk("in "),
                _chat_chunk("transit"),
                _chat_chunk(None, {"prompt_tokens": 1000, "completion_tokens": 2}),
                "[DONE]",
            )
        ],
    )

    result, report = _run(tmp_path, _spec(tmp_path, proxy=True), upstream, PROXIED_STREAM="1")

    test = _test(report)
    assert test["status"] == "failed", result.stdout + result.stderr
    assert "used 1000 input tokens" in " ".join(test["errors"])


def test_hb30_listing_the_models_is_not_read_and_not_an_error(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    reply = {**REPLY, "usage": {"prompt_tokens": 100, "completion_tokens": 2}}
    upstream.answers["/v1/chat/completions"] = Answer(_json(reply))
    upstream.answers["/v1/models"] = Answer(_json({"data": [{"id": "gpt-4o-mini"}]}))

    result, report = _run(tmp_path, _spec(tmp_path, proxy=True), upstream, PROXIED_LIST="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _test(report)["status"] == "passed"
    assert any("not read" in w and "GET /models" in w for w in report["specs"][0]["warnings"])


def test_hb30_a_turn_that_says_cost_0_does_not_get_past_max_cost(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    reply = {**REPLY, "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000}}
    upstream.answers["/v1/chat/completions"] = Answer(_json(reply))
    byok = {
        "model": "gpt-4o-mini",
        "input_tokens": 10,
        "output_tokens": 2,
        "cost": 0,
        "currency": "USD",
    }
    spec = _spec(tmp_path, proxy=True, budget="{max_input_tokens: 1000000}")

    result, report = _run(
        tmp_path, spec, upstream, "--max-cost", "0.01", PROXIED_TURN_USAGE=json.dumps(byok)
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert report["stopped"]["reason"] == "budget_exceeded"


def test_hb30_no_key_and_no_prompt_reach_the_trace_the_logs_or_the_report(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    upstream.answers["/v1/chat/completions"] = Answer(_json(REPLY))

    result, report = _run(tmp_path, _spec(tmp_path, proxy=True), upstream, "--keep-traces")

    kept = [line for line in result.stdout.splitlines() if "Traces kept at" in line]
    trace = Path(kept[0].split("Traces kept at", 1)[1].strip())
    try:
        texts = {
            "trace": trace.read_text(encoding="utf-8"),
            "report": json.dumps(report),
            "stdout": result.stdout,
            "stderr": result.stderr,
            **{
                str(log): log.read_text(encoding="utf-8", errors="replace")
                for log in (tmp_path / ".highbar" / "logs").glob("*")
            },
        }
    finally:
        trace.unlink(missing_ok=True)
    assert '"source": "proxy"' in texts["trace"]
    # The key is in the agent's Authorization header, the prompt in the request body.
    for place, text in texts.items():
        assert KEY not in text, place
        assert SYSTEM not in text, place
