"""HB-14 end to end: an agent that answers 202 and replies later, from a worker.

The agent below is the shape of Seu AgenteIA's WhatsApp path, in the standard library:
the webhook says 202 with no body, and a thread writes the turn record, with the reply,
0.3 s later. With `agent.reply: turn`, highbar checks that reply.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.integration.test_report import _free_port

QUEUE_AGENT = """\
import sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from highbar import Highbar

hb = Highbar()
REPLY = sys.argv[2]


class Agent(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        test = self.headers.get("X-Highbar-Test")
        self.send_response(202)
        self.end_headers()
        if REPLY != "-":
            threading.Thread(target=self.worker, args=(test,)).start()

    @staticmethod
    def worker(test):
        time.sleep(0.3)  # the queue
        with hb.turn(test=test) as turn:
            turn.response = REPLY

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Agent).serve_forever()
"""


def _run(tmp_path: Path, reply: str, expect: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "agent.py").write_text(QUEUE_AGENT, encoding="utf-8")
    port = _free_port()
    spec = tmp_path / "queue.highbar.yaml"
    spec.write_text(
        textwrap.dedent(f"""\
            agent:
              start: "{sys.executable} agent.py {port} '{reply}'"
              endpoint: "http://127.0.0.1:{port}/webhook"
              health: "http://127.0.0.1:{port}/health"
              settle_time: 50
              timeout: 3000
              reply: turn
            suites:
              - name: WhatsApp
                tests:
                  - trigger: "and the deadline?"
                    expect: {{response: {{contains: "{expect}"}}}}
            """),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", spec.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.timeout(40)
def test_hb14_the_reply_a_worker_writes_later_is_checked(tmp_path: Path) -> None:
    passed = _run(tmp_path, "you have 7 days", "days")
    assert passed.returncode == 0, passed.stdout + passed.stderr

    failed = _run(tmp_path, "you have 7 days", "weeks")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "1 failed" in failed.stdout, failed.stdout


@pytest.mark.timeout(40)
def test_hb14_a_reply_that_never_comes_is_an_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "-", "days")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "agent.reply: turn: no turn record of this test with a response in 3s" in result.stdout
    assert "1 error" in result.stdout, result.stdout


@pytest.mark.timeout(40)
def test_hb17_tag_picks_a_test_whose_reply_comes_from_its_turn(tmp_path: Path) -> None:
    """Tags need nothing of agent.reply: turn: the test left out is never sent."""
    (tmp_path / "agent.py").write_text(QUEUE_AGENT, encoding="utf-8")
    port = _free_port()
    spec = tmp_path / "queue.highbar.yaml"
    spec.write_text(
        textwrap.dedent(f"""\
            agent:
              start: "{sys.executable} agent.py {port} 'you have 7 days'"
              endpoint: "http://127.0.0.1:{port}/webhook"
              health: "http://127.0.0.1:{port}/health"
              settle_time: 50
              timeout: 3000
              reply: turn
            suites:
              - name: WhatsApp
                tests:
                  - trigger: "and the deadline?"
                    tags: [smoke]
                    expect: {{response: {{contains: "days"}}}}
                  - trigger: "in weeks?"
                    expect: {{response: {{contains: "weeks"}}}}
            """),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", spec.name, "--tag", "smoke"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout
    assert "in weeks?" not in result.stdout
    assert "part of the tests: 1 of 2 picked by --tag smoke, 1 left out" in result.stdout
