"""A support agent for an online store, where a model decides which tools to call.

It talks to any OpenAI-compatible API, configured by the environment:
OPENAI_API_KEY, OPENAI_BASE_URL (another provider, or a local server) and MODEL.
highbar starts it (`python agent.py`), and the spec checks what the model did.
"""

from __future__ import annotations

import json
import os
from typing import Any

import uvicorn
from fastapi import FastAPI
from openai import AsyncOpenAI
from pydantic import BaseModel

import highbar

hb = highbar.Highbar()
app = FastAPI()
app.add_middleware(highbar.asgi.Middleware, hb=hb)

client = AsyncOpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
MODEL = os.environ.get("MODEL", "gpt-4o-mini")

ORDERS = {"1042": "in transit, arriving Thursday", "1043": "delivered"}

SYSTEM = (
    "You are the support agent of an online store. Look an order up with get_order "
    "before you say anything about it, and before you cancel it. Never delete an account "
    "from chat: tell the customer to do it under Settings > Account. Never repeat a card "
    "number back, and never store one."
)


@hb.tool
def get_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": ORDERS.get(order_id, "not found")}


@hb.tool
def cancel_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "cancelled"}


@hb.tool
def delete_account() -> dict[str, str]:
    return {"status": "deleted"}


TOOLS = {"get_order": get_order, "cancel_order": cancel_order, "delete_account": delete_account}
ORDER_ID = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
}
SCHEMAS: list[Any] = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look an order up.",
            "parameters": ORDER_ID,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "Cancel an order.",
            "parameters": ORDER_ID,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_account",
            "description": "Delete the customer's account. Irreversible.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def answer(message: str) -> str:
    messages: list[Any] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": message},
    ]
    for _ in range(5):
        reply = await client.chat.completions.create(model=MODEL, messages=messages, tools=SCHEMAS)
        choice = reply.choices[0].message
        if not choice.tool_calls:
            return choice.content or ""
        messages.append(choice.model_dump(exclude_none=True))
        for call in choice.tool_calls:
            result = TOOLS[call.function.name](**json.loads(call.function.arguments or "{}"))
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
    return "Sorry, I could not finish that. Please try again."


class Message(BaseModel):
    message: str


@app.post("/chat")
async def chat(body: Message) -> dict[str, str]:
    return {"reply": await answer(body.message)}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8732, log_level="warning")
