"""A support agent for an online store. No LLM: a few rules stand in for the model.

highbar starts it (`python agent.py`), sends it messages, and checks the tool calls
it records. Set GUARDRAILS to False and run `highbar run` again to watch the
guardrail tests catch it.
"""

from __future__ import annotations

import re

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

import highbar

GUARDRAILS = True

hb = highbar.Highbar()
app = FastAPI()
app.add_middleware(highbar.asgi.Middleware, hb=hb)

ORDERS = {"1042": "in transit, arriving Thursday", "1043": "delivered"}


@hb.tool
def get_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": ORDERS.get(order_id, "not found")}


@hb.tool
def cancel_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "cancelled"}


@hb.tool
def delete_account() -> dict[str, str]:
    return {"status": "deleted"}


def answer(message: str) -> str:
    text = message.lower()
    if "delete my account" in text:
        if GUARDRAILS:
            return "I can't delete accounts from chat. You can do it under Settings > Account."
        delete_account()
        return "Done, your account is deleted."
    card = re.search(r"\d[\d -]{11,}\d", message)
    if card:
        if GUARDRAILS:
            return "I can't store card numbers. Please add the card on the payment page."
        return f"Saved your card {card.group(0)} for next time."
    order = re.search(r"order #?(\d+)", text)
    if order:
        order_id = order.group(1)
        status = get_order(order_id)["status"]
        if "cancel" in text and status != "not found":
            cancel_order(order_id)
            return f"Order {order_id} is cancelled."
        return f"Order {order_id} is {status}."
    return "I can help with orders: ask where one is, or ask me to cancel it."


class Message(BaseModel):
    message: str


@app.post("/chat")
async def chat(body: Message) -> dict[str, str]:
    return {"reply": answer(body.message)}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8731, log_level="warning")
