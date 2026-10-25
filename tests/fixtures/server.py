"""A stand-in agent for the process tests. Standard library only.

Serves ``GET /health`` -> 200 and ``POST /chat`` -> ``{"response": "ok"}``.

    --port N         port to listen on (required)
    --host H         address to listen on (default 127.0.0.1)
    --print-bytes N  write N bytes to stdout before listening
    --delay S        wait S seconds before listening
    --exit-code N    print "boom-marker" and exit with N, without listening
    --slow-chat S    POST /chat takes S seconds to answer
    --ignore-term    ignore SIGTERM (only SIGKILL stops it), like a server mid-request
"""

from __future__ import annotations

import argparse
import json
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
    args = parser.parse_args()

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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            return None

        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(args.slow_chat)
            body = json.dumps({"response": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
