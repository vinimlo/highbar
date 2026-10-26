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

With HIGHBAR_TRACES_URL set (and no --no-traces), it writes the trace contract the
way any emitter would, without the SDK: a `hello` when it starts and a `turn` per
POST, whose context has the X-Highbar-Test header and the body's conversation_id.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
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
    args = parser.parse_args()
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
            record({"type": "turn", "context": {k: v for k, v in context.items() if v is not None}})
            body = json.dumps({"response": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
