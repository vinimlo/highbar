# SDK Integration

The Highbar SDK provides the `@hb.tool` decorator that instruments your agent's tool functions for behavior testing. When tests are running, the decorator captures every tool invocation as a trace. In production, it adds zero overhead.

## Quick Start

```python
from highbar import Highbar

hb = Highbar()

@hb.tool
def search_orders(customer_id: str, status: str = "all") -> dict:
    # your implementation here
    return {"orders": [...]}
```

That is all. Your tool functions work exactly as before, and Highbar captures their invocations only when tests are running.

## How It Works

The `Highbar` class checks for the `HIGHBAR_TRACES_URL` environment variable on instantiation:

- **Variable is set**: The decorator wraps each tool function to capture traces and write them to a JSONL file at the specified path.
- **Variable is not set**: The decorator returns the original function completely untouched. There is no wrapper, no conditional check at call time, and no performance impact whatsoever.

When you run `highbar run`, the CLI automatically sets `HIGHBAR_TRACES_URL` to a temporary file path before starting your agent subprocess. You do not need to configure this yourself.

## What Gets Captured

Each tool invocation produces a trace record with these fields:

| Field | Description |
|-------|-------------|
| `fn` | The function name (e.g., `"search_orders"`) |
| `args` | Arguments bound to parameter names (e.g., `{"customer_id": "abc", "status": "all"}`) |
| `result` | The return value of the function |
| `started_at` | Unix timestamp when the function was called |
| `ended_at` | Unix timestamp when the function returned |
| `error` | Error string if the function raised an exception, otherwise omitted |

Arguments are captured using `inspect.signature` to bind both positional and keyword arguments to their parameter names. This means even positional arguments appear as named keys in the trace.

## Thread Safety

The trace writer uses a `threading.Lock` to protect the sequence counter and file writes. Multiple tool calls running concurrently in different threads will produce correctly ordered, non-interleaved trace records.

## Full Example: FastAPI Agent

Here is a complete agent with two decorated tools, ready for Highbar testing:

```python
from fastapi import FastAPI
from pydantic import BaseModel
from highbar import Highbar

app = FastAPI()
hb = Highbar()


# --- Tools ---

@hb.tool
def search_orders(customer_id: str, status: str = "active") -> dict:
    """Search orders by customer ID and optional status filter."""
    # In a real agent, this would query a database
    return {
        "orders": [
            {"order_id": "ORD-001", "status": "shipped", "total": 59.99},
            {"order_id": "ORD-002", "status": "delivered", "total": 129.00},
        ]
    }


@hb.tool
def initiate_refund(order_id: str, reason: str = "customer_request") -> dict:
    """Start a refund for the given order."""
    return {
        "refund_id": "REF-100",
        "order_id": order_id,
        "status": "processing",
    }


# --- Agent endpoint ---

class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    # Your LLM orchestration logic here.
    # Call search_orders() or initiate_refund() based on the user's message.
    response_text = handle_message(req.message)
    return {"response": response_text}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def handle_message(message: str) -> str:
    """Placeholder for your LLM-based routing logic."""
    if "order" in message.lower():
        result = search_orders(customer_id="cust-123")
        return f"Found {len(result['orders'])} orders."
    return "How can I help you today?"
```

A matching spec file to test this agent:

```yaml
agent:
  start: "uvicorn my_agent:app --port 8000"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Order Lookup
    tests:
      - trigger: "Show me my recent orders"
        expect:
          function_calls:
            - name: search_orders
              params:
                customer_id: "cust-123"
          response:
            contains: "orders"
```

## Production Safety

The decorator is designed for production use. When `HIGHBAR_TRACES_URL` is not set:

- No `TraceWriter` is created
- `make_tool_decorator` receives `writer=None` and returns the original function directly
- There is no wrapper function, no conditional branch at call time, and no overhead
- The `@hb.tool` line is functionally equivalent to a no-op

This means you can ship the decorator in your production code without any performance concerns.

## Explicit Trace Path

For testing or development, you can pass a trace file path directly instead of relying on the environment variable:

```python
hb = Highbar(traces="/tmp/my-traces.jsonl")
```

This is useful for:
- Unit testing the SDK itself
- Debugging trace output
- Running traces outside of the Highbar CLI
