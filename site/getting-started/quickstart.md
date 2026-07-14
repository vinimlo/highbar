# Quickstart

Build and test your first AI agent in 5 minutes.

## 1. Install Gauntlet

```bash
pip install gauntlet
```

## 2. Create Your Agent

Create a file called `agent.py` with a minimal FastAPI agent that looks up order status:

```python
from fastapi import FastAPI
from gauntlet import Gauntlet
import uvicorn

app = FastAPI()
gt = Gauntlet()

@gt.tool
def search_orders(order_id: str) -> dict:
    return {"order_id": order_id, "status": "shipped"}

@app.post("/chat")
async def chat(request: dict) -> dict:
    message = request.get("message", "")
    if "order" in message.lower() and "#" in message:
        order_id = message.split("#")[1].split()[0].strip("?.,!")
        result = search_orders(order_id=order_id)
        return {"response": f"Order #{order_id} status: {result['status']}"}
    return {"response": "I can help with order inquiries."}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, port=8000)
```

The key line is `@gt.tool`. This decorator instruments the `search_orders` function so that every invocation is captured as a trace — function name, arguments, return value, and timing. When `GAUNTLET_TRACES_URL` is not set (i.e., normal production use), the decorator is a no-op.

## 3. Write a Spec

Create the file `specs/orders.gauntlet.yaml`:

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Order Lookup
    tests:
      - trigger: "Where is my order #12345?"
        expect:
          function_calls:
            - name: search_orders
              params:
                order_id: "12345"
          response:
            contains: "shipped"
```

This spec tells Gauntlet:

- **How to start the agent** — run `python agent.py` as a subprocess
- **Where to send triggers** — POST to `http://localhost:8000/chat`
- **How to check readiness** — GET `http://localhost:8000/health`
- **What to assert** — the agent should call `search_orders` with `order_id: "12345"` and the response should contain "shipped"

## 4. Run the Tests

```bash
gauntlet run specs/orders.gauntlet.yaml
```

Expected output:

```
  Gauntlet v0.1.0
  Running specs/orders.gauntlet.yaml

  Order Lookup
    ✓ Where is my order #12345?

  1 passed, 0 failed (0.8s)
```

## 5. What Just Happened

Here is exactly what Gauntlet did:

1. **Parsed the spec** — read `orders.gauntlet.yaml` and validated the schema.
2. **Created a trace file** — generated a temporary JSONL file for trace collection.
3. **Started the agent** — launched `python agent.py` as a subprocess with the `GAUNTLET_TRACES_URL` environment variable pointing to the trace file.
4. **Waited for health** — polled `GET /health` until the agent responded (up to 15 seconds by default).
5. **Sent the trigger** — sent `POST /chat` with `{"message": "Where is my order #12345?"}`.
6. **Waited for settle** — paused 500ms (configurable) to allow async tool calls to complete.
7. **Collected traces** — read the JSONL trace file to find all tool invocations.
8. **Ran assertions** — checked that `search_orders` was called with `order_id: "12345"` and the response contained "shipped".
9. **Reported results** — printed the results to the terminal.
10. **Stopped the agent** — terminated the subprocess.

## 6. Add a Guardrail Test

Add a second test that ensures the agent does not call any tools when the user asks something unrelated:

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Order Lookup
    tests:
      - trigger: "Where is my order #12345?"
        expect:
          function_calls:
            - name: search_orders
              params:
                order_id: "12345"
          response:
            contains: "shipped"

      - trigger: "What is the weather today?"
        expect:
          function_calls: []
```

The `function_calls: []` assertion means "assert that no tools were called." This is how you write guardrail tests — validating that your agent does **not** do something it should not.

## Next Steps

- [Core Concepts](/getting-started/concepts) — understand specs, traces, and the execution model
- [Introduction](/getting-started/introduction) — learn about Gauntlet's philosophy and design
