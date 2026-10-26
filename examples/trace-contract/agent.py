"""The quickstart's support agent without the highbar package.

Plain standard-library HTTP, and the trace contract written by emitter.py: what an
agent in any language does to be tested by highbar.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import emitter

ORDERS = {"1042": "in transit, arriving Thursday", "1043": "delivered"}


def get_order(order_id: str, test: str | None) -> dict[str, str]:
    started = time.time()
    result = {"order_id": order_id, "status": ORDERS.get(order_id, "not found")}
    emitter.tool("get_order", {"order_id": order_id}, result=result, started_at=started, test=test)
    return result


def answer(message: str, test: str | None) -> str:
    if "delete" in message.lower():
        return "I can't delete accounts from chat. You can do it under Settings > Account."
    for order_id in ORDERS:
        if order_id in message:
            return f"Order {order_id} is {get_order(order_id, test)['status']}."
    return "How can I help with your order?"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # the health check
        self._reply({"status": "ok"})

    def do_POST(self) -> None:
        test = self.headers.get("X-Highbar-Test")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            reply = answer(body["message"], test)
        finally:
            emitter.turn(test)
        self._reply({"reply": reply})

    def _reply(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


if __name__ == "__main__":
    emitter.hello()
    ThreadingHTTPServer(("127.0.0.1", 8733), Handler).serve_forever()
