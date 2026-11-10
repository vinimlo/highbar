"""A stand-in agent for the process tests. Standard library only.

Serves ``GET /health`` -> 200 and ``POST /chat`` -> ``{"response": "ok"}``.

    --port N         port to listen on (required)
    --host H         address to listen on (default 127.0.0.1)
    --print-bytes N  write N bytes to stdout before listening
    --delay S        wait S seconds before listening
    --exit-code N    print "boom-marker" and exit with N, without listening
    --slow-chat S    POST /chat takes S seconds to answer
    --ignore-term    ignore SIGTERM (only SIGKILL stops it), like a server mid-request
    --no-traces      write no trace records, like an agent without the SDK
    --replies A,B    POST /chat answers A, then B, and so on, then A again
                     (default: ok), like an agent that is right only some of the time
    --late-tool T:S  S seconds after answering a message with "late" in it, record a
                     call to tool T, like work the agent left running in the background
    --cost C         each `turn` reports a usage of 100 input tokens, 10 output tokens
                     and C USD
    --stray-cost C   each POST also writes a `turn` with no context and that usage, at
                     C USD, like an hb.turn() in a thread that lost it
    --statuses A,B   POST /chat answers with A, then B, and so on, then A again (default:
                     200), like an agent whose infrastructure is down: 200 answers as
                     above; another HTTP status writes no record and answers a JSON body
                     with "upstream exploded" and a request id that changes every time;
                     "hang" answers nothing for --hang seconds (default 3), past a short
                     agent.timeout

With HIGHBAR_TRACES_URL set (and no --no-traces), it writes the trace contract the
way any emitter would, without the SDK: a `hello` when it starts and a `turn` per
POST, whose context has the X-Highbar-Test header and the body's conversation_id.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--print-bytes", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--exit-code", type=int, default=None)
    parser.add_argument("--slow-chat", type=float, default=0.0)
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--no-traces", action="store_true")
    parser.add_argument("--replies", default="ok")
    parser.add_argument("--late-tool", default=None)
    parser.add_argument("--cost", type=float, default=None)
    parser.add_argument("--stray-cost", type=float, default=None)
    parser.add_argument("--statuses", default="200")
    parser.add_argument("--hang", type=float, default=3.0)
    args = parser.parse_args()
    replies = itertools.cycle(args.replies.split(","))
    statuses = itertools.cycle(args.statuses.split(","))
    replies_lock = threading.Lock()
    traces = None if args.no_traces else os.environ.get("HIGHBAR_TRACES_URL")

    def record(entry: dict[str, object]) -> None:
        if traces:
            with open(traces, "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "ts": time.time(), **entry}) + "\n")

    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    if args.exit_code is not None:
        print("boom-marker", flush=True)
        sys.exit(args.exit_code)

    remaining = args.print_bytes
    chunk = b"x" * 65536
    while remaining > 0:
        sys.stdout.buffer.write(chunk[: min(remaining, len(chunk))])
        remaining -= len(chunk)
    sys.stdout.buffer.flush()

    time.sleep(args.delay)
    record({"type": "hello", "sdk": "test-server/1", "pid": os.getpid()})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            return None

        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with replies_lock:
                status = next(statuses)
            if status == "hang":
                # Answered by nothing: the connection closes once the client gave up.
                time.sleep(args.hang)
                return
            if status != "200":
                failure = {"error": "upstream exploded", "request_id": uuid.uuid4().hex}
                body = json.dumps(failure).encode()
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            time.sleep(args.slow_chat)
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            context = {
                "test": self.headers.get("X-Highbar-Test"),
                "conversation_id": payload.get("conversation_id")
                if isinstance(payload, dict)
                else None,
            }
            tags = {k: v for k, v in context.items() if v is not None}
            usage = {"input_tokens": 100, "output_tokens": 10, "cost": args.cost, "currency": "USD"}
            record({"type": "turn", "context": tags, "usage": usage if args.cost else None})
            if args.stray_cost:
                record({"type": "turn", "usage": {**usage, "cost": args.stray_cost}})
            if args.late_tool and "late" in str(payload):
                tool, seconds = args.late_tool.split(":")
                late = {"type": "tool", "fn": tool, "args": {}, "context": tags}
                threading.Timer(float(seconds), record, [late]).start()
            with replies_lock:
                reply = next(replies)
            body = json.dumps({"response": reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
