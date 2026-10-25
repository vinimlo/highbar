"""Minimal test agent for integration testing."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from highbar import Highbar

app = FastAPI()
hb = Highbar()


@hb.tool
def search_orders(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


@hb.tool
def initiate_refund(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "refund_status": "initiated"}


@app.post("/chat")
async def chat(request: dict[str, str]) -> dict[str, str]:
    message = request.get("message", "")

    if "order" in message.lower() and "#" in message:
        order_id = message.split("#")[1].split()[0].strip("?.,!")
        result = search_orders(order_id=order_id)
        return {"response": f"Your order #{order_id} status: {result['status']}"}

    if "refund" in message.lower():
        result = initiate_refund(order_id="12345")
        return {"response": f"Refund initiated for order #{result['order_id']}"}

    return {"response": "I can only help with order-related questions"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8111)
