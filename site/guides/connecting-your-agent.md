# Connecting your agent

highbar learns what the agent did from a trace file the agent writes: one JSON line per tool call (`tool`) and one per message handled (`turn`). The file's location reaches the agent in one of two ways:

- **highbar starts the agent** (`agent.start`): it creates a fresh file and passes its path in the `HIGHBAR_TRACES_URL` environment variable.
- **highbar attaches to a running agent**: the agent writes where you configure it, and the spec says where in `agent.traces`.

With nothing configured, the SDK writes nothing and costs nothing, so the same code runs in production.

There are three ways to write the file.

## 1. Decorate your tools (Python)

```python
import highbar

hb = highbar.Highbar()  # reads HIGHBAR_TRACES_URL; or Highbar(traces="/var/log/agent/traces.jsonl")


@hb.tool
def get_order(order_id: str) -> dict:
    return db.orders.get(order_id)


@hb.tool
async def cancel_order(order_id: str) -> dict:
    return await payments.cancel(order_id)
```

Each call is recorded with its arguments (by name), its result, and its start and end times. Sync and async functions both work. When the tool raises, the record gets `error: "TypeName: message"` and the exception goes on as before; nothing in the SDK raises into your agent.

### Tag each message with its test

Each test sends its id in the `X-Highbar-Test` header. The calls made while that request is handled must carry it, or highbar cannot tell this test's calls from a slow call of the previous one. For FastAPI, Starlette or any ASGI app, one line does it:

```python
app.add_middleware(highbar.asgi.Middleware, hb=hb)
```

The middleware writes a `turn` record for every request, and every tool call made while the request is handled carries the test id in its `context`.

In another framework, wrap the handling of a message in `hb.turn()`:

```python
def handle(request):
    with hb.turn(test=request.headers.get("x-highbar-test"), message=request.json["message"]) as turn:
        reply = agent.respond(request.json["message"])
        turn.response = reply
    return {"reply": reply}
```

The tag lives in a `contextvars` context, so it follows `await` but not a thread the agent starts itself. Start such threads with `contextvars.copy_context().run(...)`, or record the call with the tag explicitly (below).

## 2. Record calls from a funnel (Python)

When tools are data, such as rows in a database or a plugin registry dispatched by one function, there is no function per tool to decorate. Record each call where it is dispatched:

```python
def dispatch(name: str, args: dict, conversation_id: str) -> dict:
    result = TOOLS[name].run(args)
    hb.record(
        name,
        args,
        result=result,
        error=result.get("error"),  # a failure the tool returned instead of raising
        conversation_id=conversation_id,
    )
    return result
```

Extra keyword arguments go into the record's `context`. highbar never reads `result` to decide whether a call failed: a failure must be in `error`.

If the test id cannot reach the funnel but a conversation id can, correlate on it instead: <code v-pre>agent.correlate: {conversation_id: "{{session.conversation_id}}"}</code>. See [attach mode and sessions](/guides/attach-mode).

## 3. Write the contract (any language)

The file format is small and versioned: one JSON object per line, `v: 1`, a `type`, and the fields of that type. An agent in TypeScript, Go or Java writes it directly, to the file named by `HIGHBAR_TRACES_URL`: a `hello` when it starts, then a `tool` per call and a `turn` per message. When highbar starts the agent, a run without the `hello` stops with exit code 2.

```jsonl
{"v": 1, "type": "hello", "ts": 1792983750.122, "sdk": "my-agent/1.0", "pid": 94015}
{"v": 1, "type": "tool", "ts": 1792983750.215, "fn": "get_order", "args": {"order_id": "1042"}, "result": {"status": "in transit"}, "started_at": 1792983750.214, "ended_at": 1792983750.215, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
{"v": 1, "type": "turn", "ts": 1792983750.216, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

The [trace contract](/reference/trace-contract) has every field, and a reference emitter in under 40 lines.

## Checking the connection

Run the quick start's spec against your agent with a single guardrail test first. If the agent is connected, the test passes; if it is not, the run says what highbar read and what it expected. Add `--keep-traces` to look at the file yourself. [Troubleshooting](/guides/troubleshooting) lists the messages and what each one means.
