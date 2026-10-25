# SDK API Reference

The Highbar SDK provides a lightweight decorator for instrumenting agent tool functions. When tracing is active, each tool invocation is captured as a trace box and written to a JSONL file. When tracing is disabled, the decorator has zero overhead.

## Installation

```bash
# Full install (CLI + SDK + all dependencies)
pip install highbar

# Slim install for production (decorator only, zero extra deps)
pip install highbar[sdk]
```

## Quick Start

```python
from highbar import Highbar

hb = Highbar()

@hb.tool
def search_orders(order_id: str) -> dict:
    # Your existing tool logic — unchanged
    return {"order_id": order_id, "status": "shipped"}

@hb.tool
def initiate_refund(order_id: str, reason: str) -> dict:
    return {"refund_id": "REF-001", "status": "pending"}
```

When the agent runs under Highbar (with `HIGHBAR_TRACES_URL` set), every call to `search_orders` or `initiate_refund` writes a trace record. In production (without the env var), the decorator passes through with no effect.

---

## `Highbar` class

**Module:** `highbar.sdk.core`

```python
class Highbar:
    def __init__(self, traces: str | None = None) -> None: ...

    @property
    def traces(self) -> str | None: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def tools(self) -> dict[str, Callable[..., Any]]: ...

    def tool(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
```

### Constructor

```python
hb = Highbar(traces="/tmp/traces.jsonl")
# or
hb = Highbar()  # reads from HIGHBAR_TRACES_URL env var
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `traces` | `str \| None` | `None` | Path to the JSONL trace file. If not provided, reads from the `HIGHBAR_TRACES_URL` environment variable. If neither is set, tracing is disabled. |

### Properties

#### `traces -> str | None`

Returns the path to the trace file, or `None` if tracing is disabled.

#### `enabled -> bool`

Returns `True` when tracing is active (a trace file path is set). Use this to conditionally execute tracing-related logic in your agent.

```python
hb = Highbar()
if hb.enabled:
    print(f"Tracing to {hb.traces}")
```

#### `tools -> dict[str, Callable[..., Any]]`

Returns a dictionary of all registered tool functions, keyed by function name. Each value is the wrapped callable. Returns a copy to prevent external mutation.

```python
hb = Highbar()

@hb.tool
def search_orders(order_id: str) -> dict:
    return {}

print(hb.tools)
# {'search_orders': <function search_orders at 0x...>}
```

### Methods

#### `tool(fn) -> Callable`

Decorator that registers a function as a Highbar tool and wraps it to capture trace records on each invocation.

```python
@hb.tool
def my_function(arg1: str, arg2: int) -> dict:
    return {"result": "value"}
```

**Behavior:**

- Registers the function in the `tools` dictionary under its `__name__`.
- When tracing is enabled, each call writes a trace box to the JSONL file containing the function name, arguments, return value, timing, and any error.
- When tracing is disabled, the function is called directly with no overhead.
- Exceptions are captured in the trace (with the `error` field) and then re-raised.

---

## `TraceWriter` class

**Module:** `highbar.sdk.trace`

The `TraceWriter` is an internal class used by the `Highbar` SDK to write trace boxes. You typically do not interact with it directly.

```python
class TraceWriter:
    def __init__(self, path: str) -> None: ...

    def write(
        self,
        fn: str,
        args: dict[str, object],
        result: object,
        started_at: float | None = None,
        ended_at: float | None = None,
        error: str | None = None,
    ) -> str: ...
```

### Constructor

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | `str` | Absolute path to the JSONL trace file |

### Methods

#### `write(...) -> str`

Writes a single trace box to the JSONL file. Returns the fingerprint ID of the trace.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fn` | `str` | - | Function name |
| `args` | `dict[str, object]` | - | Function arguments |
| `result` | `object` | - | Return value |
| `started_at` | `float \| None` | `None` | Start timestamp (`time.time()`). Defaults to current time. |
| `ended_at` | `float \| None` | `None` | End timestamp. Defaults to current time. |
| `error` | `str \| None` | `None` | Error message if the function raised an exception |

### Fingerprint Generation

Each trace box receives a unique fingerprint ID computed as:

```
sha256(json({"fn": fn, "args": args, "nonce": uuid4_hex}))[:16]
```

The nonce ensures uniqueness even for identical function calls with the same arguments.

### Thread Safety

`TraceWriter` is thread-safe. All writes are serialized via a `threading.Lock`, ensuring both the sequence counter and file writes are atomic. This means `@hb.tool`-decorated functions can safely be called from multiple threads concurrently.
