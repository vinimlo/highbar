"""A support agent whose behavior comes from its system prompt. No LLM: rules stand in.

A real agent sends the prompt to a model with each message. This one reads the prompt
for a few sentences it knows and follows them, so a prompt change changes what it does
in a way that is the same on every run, with no model and no key. The prompt's path is
in PROMPT_FILE, which loop.py sets for each prompt it tries.

Standard library HTTP; the highbar SDK records the tool calls and one turn per message.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import highbar

hb = highbar.Highbar()

ORDERS = {"1042": "in transit, arriving Thursday", "1043": "delivered"}
LATEST_ORDER = "1043"  # the customer's last order: what a guess lands on
TEAMS = {"support", "returns"}


class Rules:
    """What the prompt asks for, read from its sentences."""

    def __init__(self, prompt: str) -> None:
        text = " ".join(prompt.lower().split())
        self.look_up_first = "look the order up before you refund it" in text
        self.order_id_first = "never refund without an order id" in text
        self.refunds_to_human = "hand every refund to a human" in text
        angry = re.search(r"hand angry customers to the ([a-z]+) team", text)
        self.angry_team = angry.group(1) if angry else None


@hb.tool
def get_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": ORDERS.get(order_id, "not found")}


@hb.tool
def refund_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "refunded"}


@hb.tool
def escalate_to_human(team: str, reason: str) -> dict[str, str]:
    if team not in TEAMS:
        # Not caught: the message ends in HTTP 500, which highbar reports as an error.
        raise ValueError(f"no team called {team!r}")
    return {"team": team, "ticket": "T-1"}


def answer(message: str, rules: Rules) -> str:
    text = message.lower()
    order = re.search(r"order #?(\d+)", text)
    angry = any(word in text for word in ("furious", "third time", "unacceptable"))
    if angry and rules.angry_team:
        escalate_to_human(team=rules.angry_team, reason="angry customer")
        return f"I'm sorry. I've passed this to our {rules.angry_team} team; they'll write today."
    if "refund" in text:
        if rules.refunds_to_human:
            escalate_to_human(team="support", reason="refund")
            return "A colleague will look at your refund and write to you."
        if order is None:
            if rules.order_id_first:
                return "Which order is it? Send me the order number and I'll refund it."
            order_id = LATEST_ORDER  # nothing told it to ask, so it guesses
        else:
            order_id = order.group(1)
        if rules.look_up_first:
            get_order(order_id)
        refund_order(order_id)
        return f"Order {order_id} is refunded."
    if order:
        order_id = order.group(1)
        return f"Order {order_id} is {get_order(order_id)['status']}."
    return "I can help with orders: ask where one is, or ask for a refund."


def usage(prompt: str, message: str, reply: str) -> dict[str, Any]:
    """What a model's response reports. This agent has no model, so it counts words."""
    input_tokens = len(prompt.split()) + len(message.split())
    output_tokens = len(reply.split())
    return {
        "model": "rules",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": (input_tokens * 3 + output_tokens * 15) / 1_000_000,
        "currency": "USD",
    }


def serve(prompt: str, port: int) -> None:
    rules = Rules(prompt)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the health check
            self._reply(200, {"status": "ok"})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            message = str(body.get("message", ""))
            # The turn is written when the block ends, before the reply goes out.
            with hb.turn(test=self.headers.get("X-Highbar-Test"), message=message) as turn:
                try:
                    reply = answer(message, rules)
                    status, out = 200, {"reply": reply}
                    turn.response = reply
                except Exception as e:
                    reply = ""
                    status, out = 500, {"error": str(e)}
                turn.usage = usage(prompt, message, reply)
            self._reply(status, out)

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    path = os.environ.get("PROMPT_FILE") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not path:
        sys.exit("agent.py needs its system prompt: set PROMPT_FILE, or pass the file's path")
    serve(Path(path).read_text(encoding="utf-8"), port=8734)
