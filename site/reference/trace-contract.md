# Trace contract v1

The trace file is how an agent tells highbar what it did. The Python SDK writes it; an agent in any other language writes it directly. The format is versioned so that both sides can change without breaking each other.

## The file

- One JSON object per line (JSON Lines), UTF-8, each line ending in a newline.
- The agent appends; highbar reads from where it stopped last time. A line not yet ended by a newline is left for the next read, so a writer may be in the middle of one.
- A file that becomes smaller than where highbar stopped was recreated (a restarted agent, a rotated log), and is read from the start.
- A line that is not valid JSON, a `tool` record without `fn`, or `args` or `context` that are not objects, is an error for the test that read it, naming the line. It is never skipped: it may be the call the test is about. A timestamp that is not a number falls back to `ts`.

## Common fields

| Field | Type | |
|---|---|---|
| `v` | integer | `1`. A version newer than highbar reads is an error that says to upgrade highbar. |
| `type` | string | Required: `"tool"`, `"turn"`, `"hello"` or `"tools"`. A line with another type, or none, is skipped, so emitters can add new types; a tool call written without `type` is lost. |
| `ts` | number | When the record was written, in seconds since the epoch. |
| `context` | object | Optional. What links the record to a test: `test` (the `X-Highbar-Test` header) and any key `agent.correlate` uses, like `conversation_id`. |

Unknown fields are ignored.

## `tool`: one tool call

| Field | Type | |
|---|---|---|
| `fn` | string | The tool's name. Required. |
| `args` | object | The arguments, by name. |
| `result` | any JSON | What the tool returned. highbar never reads it to decide whether the call failed. |
| `error` | string or null | Why the call failed. `null`, `false` or `""` mean it did not; a value that is not text is kept as JSON. |
| `started_at`, `ended_at` | number | Seconds since the epoch. `ordered` compares them. They default to `ts`. |

```json
{"v": 1, "type": "tool", "ts": 1792983750.215, "fn": "get_order", "args": {"order_id": "1042"}, "result": {"status": "in transit"}, "error": null, "started_at": 1792983750.214, "ended_at": 1792983750.215, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

A call that failed has its `error`, even when the tool returned the failure as data instead of raising:

```json
{"v": 1, "type": "tool", "ts": 1792983751.002, "fn": "get_order", "args": {"order_id": "1042"}, "result": null, "error": "TimeoutError: upstream timed out", "started_at": 1792983750.001, "ended_at": 1792983751.002, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

## `turn`: one message handled

| Field | Type | |
|---|---|---|
| `message` | string | Optional: the message the agent received. |
| `response` | string | Optional: what it answered. |

A `turn` record is the proof that the agent handled a message. Against an attached agent, a test that checks tool calls needs a record of its own, `turn` or `tool`: without one, "no tool was called" could just mean "the agent never saw this message". Write it at the end of the handling, before the reply goes out, and write it even when the handling fails.

```json
{"v": 1, "type": "turn", "ts": 1792983750.216, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

## `hello`: the writer started

| Field | Type | |
|---|---|---|
| `sdk` | string | The writer and its version, like `"highbar-python/0.1.0"`. |
| `pid` | integer | The writer's process id. |
| `tools` | list of strings | Optional: the tools it records, as in a `tools` record. |

```json
{"v": 1, "type": "hello", "ts": 1792983750.122, "sdk": "my-agent/1.0", "pid": 94015, "tools": ["get_order", "cancel_order"]}
```

Write it once, when the agent starts. When highbar starts the agent and a test checks tool calls, highbar waits up to 2 seconds after the agent is ready for a `hello` in the new file; without one, the run stops with exit code 2. Records of other types do not count: a `hello` says the writer started, before any message arrives.

## `tools`: the tools it records

| Field | Type | |
|---|---|---|
| `names` | list of strings | Required: tool names, as they appear in `fn`. |

```json
{"v": 1, "type": "tools", "ts": 1792983750.124, "names": ["cancel_order", "get_order"]}
```

A writer may write several; highbar joins them, and adds every name it sees in a `tool` record. The Python SDK writes one each time a tool is decorated or declared, since its `hello` comes first. A name in a spec's `forbid` or `function_calls` that is in none of them makes the test an `error`: a guardrail on a tool the agent never records could not fail. A writer that declares nothing gets a warning instead, and older versions of highbar ignore the record.

## Matching records to a test

Each test execution sends its id in the `X-Highbar-Test` header. A writer copies it into `context.test` of every record made while handling that request. highbar keeps the records whose `context` matches `agent.correlate` (by default, `test`); numbers and text that read the same match, so `42` matches `"42"`.

## A reference emitter

[`examples/trace-contract`](https://github.com/vinimlo/highbar/tree/main/examples/trace-contract) is an agent without the highbar package: standard-library HTTP, and this file, which writes the contract. The same lines in any language connect an agent:

<!-- file: examples/trace-contract/emitter.py -->
```python
"""highbar's trace contract v1, written without the highbar package.

Copy it, or write the same lines in any language: one JSON object per line,
appended to the file named by HIGHBAR_TRACES_URL. Unset, nothing is written.
"""

import json
import os
import time


def _write(record: dict, test: str | None = None) -> None:
    path = os.environ.get("HIGHBAR_TRACES_URL")
    if not path:
        return
    line = {"v": 1, "ts": time.time(), **record}
    if test:  # the X-Highbar-Test header of the request being handled
        line["context"] = {"test": test}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:  # one write per line, so lines from two threads never interleave
        os.write(fd, (json.dumps(line) + "\n").encode())
    finally:
        os.close(fd)


def hello(tools: list[str]) -> None:
    """When the agent starts, with the tools it records: highbar waits for it."""
    _write({"type": "hello", "sdk": "reference-emitter/1", "pid": os.getpid(), "tools": tools})


def tool(fn, args, *, result=None, error=None, started_at, test=None) -> None:
    """One tool call. `error` says why it failed, also when it returned the failure."""
    record = {"fn": fn, "args": args, "result": result, "error": error}
    _write({"type": "tool", **record, "started_at": started_at, "ended_at": time.time()}, test)


def turn(test: str | None) -> None:
    """One message handled: before the reply goes out, also when the handling failed."""
    _write({"type": "turn"}, test)
```

The agent calls `hello(TOOLS)` before it listens, with the tools it records, `tool()` for each call, and `turn()` for each message, with the `X-Highbar-Test` header it received.

## Version 0

A line without `v` (or with `"v": 0`) is read as a v0 tool call: `fn`, `args`, `result`, `error`, `started_at` and `ended_at`, with every other top-level field (such as `conversation_id`) as its `context`. Version 0 has no `turn` records, so in attach mode its tests can check replies and calls, but a guardrail that expects no call needs the emitter to add a v1 `turn` line per message.
