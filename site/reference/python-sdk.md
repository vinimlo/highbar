# Python SDK

```python
import highbar

hb = highbar.Highbar()
```

`import highbar`, `highbar.Highbar` and `highbar.asgi` load only the Python standard library, so the SDK can live in the agent's production image. Nothing in it raises into the agent: a record that cannot be written is logged to the `highbar` logger and dropped.

## `Highbar(traces=None)`

The object that writes the trace file.

- `traces`: the file to append to. Without it, `HIGHBAR_TRACES_URL` is used, which highbar sets for an agent it starts. With neither, the SDK records nothing.
- `hb.enabled`: whether a trace file is set.
- `hb.traces`: the file in use, or `None`.

When a file is set at construction, a `hello` record is written to it.

## `@hb.tool`

```python
@hb.tool
def get_order(order_id: str) -> dict: ...


@hb.tool
async def search_products(query: str, limit: int = 10) -> list[dict]: ...
```

Records each call as a `tool` record: the arguments bound to their names (defaults included), the result, the start and end times, and the context of the message being handled. When the function raises, the record gets `error: "TypeName: message"` and the exception propagates unchanged. A coroutine is awaited before its result is recorded.

Decorating a function also declares it: the SDK writes a `tools` record with every tool it records, which highbar checks a spec's tool names against.

## `hb.record(fn, args=None, *, result=None, error=None, started_at=None, ended_at=None, **context)`

```python
hb.record("search_products", {"query": "dress"}, result=rows, conversation_id=cid)
```

Records a call made where no function can be decorated, such as a dispatch funnel. Keyword arguments beyond the named ones go into the record's `context`, on top of the current message's context. A failure the tool returned as data belongs in `error`: highbar never reads `result` to decide whether a call failed. Times default to now.

## `hb.declare(*names)`

```python
hb.declare("search_products", "add_to_cart")
```

Declares tools that `hb.record()` writes, which no decorator announces. highbar checks every name in a spec's `forbid` and `function_calls` against the tools the agent declared or was seen calling: a name it cannot see makes the test an `error`, because a guardrail on a tool the agent never records could not fail. Call it when the agent starts, with every tool the funnel can dispatch.

## `hb.turn(*, message=None, **context)`

```python
with hb.turn(test=request.headers.get("x-highbar-test"), message=text) as turn:
    reply = agent.respond(text)
    turn.response = reply
```

Marks the handling of one message. Tool calls inside the block carry its context, and a `turn` record is written when the block ends, even if it raises. Set `turn.response` to record the reply.

## `highbar.asgi.Middleware`

```python
app.add_middleware(highbar.asgi.Middleware, hb=hb)
```

For FastAPI, Starlette and any ASGI app: the same as wrapping every HTTP request in `hb.turn(test=<X-Highbar-Test header>)`. The `turn` record is written before the last part of the response is sent, so it is in the file when highbar reads it. With tracing off, the middleware does nothing.

## Context across threads

The current message's context lives in a `contextvars` variable. It follows `await` and tasks created from the handler, but not a thread the agent starts itself. Start such a thread with `contextvars.copy_context().run(target)`, or pass the context to `hb.record()` explicitly.
