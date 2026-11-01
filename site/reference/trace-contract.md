# Trace contract v1

The trace file is how an agent tells highbar what it did. The Python SDK writes it; an agent in any other language writes it directly. The format is versioned so that both sides can change without breaking each other.

## The file

- One JSON object per line (JSON Lines), UTF-8, each line ending in a newline.
- The agent appends; highbar reads from where it stopped last time. At the start of a run it also scans the file once, for the tools declared since the last `hello`. A line not yet ended by a newline is left for the next read, so a writer may be in the middle of one.
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
| `usage` | object | Optional: what handling the message consumed. See below. |

A `turn` record is the proof that the agent handled a message. Against an attached agent, a test that checks tool calls needs a record of its own, `turn` or `tool`: without one, "no tool was called" could just mean "the agent never saw this message". Write it at the end of the handling, before the reply goes out, and write it even when the handling fails.

```json
{"v": 1, "type": "turn", "ts": 1792983750.216, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

### `usage`: tokens and cost

`usage` is what the report adds up, and what a [`budget`](/reference/spec-format#budget) checks. It is optional, and so is every field in it: a writer without it works as before, and older versions of highbar ignore it. The contract stays at v1.

| Field | Type | |
|---|---|---|
| `model` | string | The model that answered, such as `gpt-4o-mini`. Looked up in the price table when `cost` is missing. |
| `provider` | string | Who served it, such as `openai`. Tells apart a model name that several providers use. |
| `input_tokens` | integer | The whole input, **cached tokens included**. |
| `output_tokens` | integer | |
| `cached_input_tokens` | integer | How many of `input_tokens` came from a cache. |
| `cost` | number | What the message cost. |
| `currency` | string | The currency of `cost`, such as `USD`. Compared without regard to case. |
| `llm_calls` | integer | How many model calls the message took. |

```json
{"v": 1, "type": "turn", "ts": 1792983750.216, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}, "usage": {"model": "gpt-4o-mini", "provider": "openai", "input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 800, "cost": 0.00021, "currency": "USD", "llm_calls": 2}}
```

::: warning The input includes the cached tokens
Not every provider counts them that way. OpenAI's `prompt_tokens` already includes `cached_tokens`, but Anthropic's `input_tokens` leaves out `cache_read_input_tokens` and `cache_creation_input_tokens`: add them up before writing the field, or `max_input_tokens` will pass on a context bigger than it allows.
:::

A value that cannot be read counts as missing, and the line is still read: a `usage` that is not an object, a negative count, text where a number goes, `NaN`. A budget on a missing value is an `error`, never a pass.

When several `turn` records of one message have `usage`, such as `highbar.asgi.Middleware` next to `hb.turn()`, they are added up, because all of it was spent. A value they disagree on, like the model, is unknown in the turn's `usage` in the report; the sums and budgets read each record, so a value one of them knows is never lost. A `usage` that cannot be read at all, next to one that can, counts as a record with nothing known. A `turn` without `usage`, or with an empty one like `null` or `{}`, counts for nothing.

A `turn` with `usage` but no test context, next to records that have one, counts for no test, as an untagged tool call does. It is left out of the sums, which say `complete: false`, and a test whose budget holds only without it is an `error`. A `hb.turn()` in a thread the agent starts itself needs `contextvars.copy_context()` to keep the tag.

A `cost` from the writer always stands. Without one, highbar works it out from `src/highbar/engine/prices.json`, a table of list prices in USD per token, in the format of [OpenRouter's model list](https://openrouter.ai/api/v1/models). The price of cached tokens applies to `cached_input_tokens`. It needs `model`, `input_tokens` and `output_tokens`; it uses the table's currency, whatever `currency` says; and it finds the model by its exact name, ignoring case and with `.` the same as `-`, so a dated snapshot like `gpt-4o-2024-05-13` is not its alias. A model without a price leaves the cost missing, with a warning. `provider` is required when two providers in the table use the name. An agent on prices of its own, or on a model outside the table, writes `cost` and `currency`.

Write `turn` before the reply goes out. A `turn` with `usage` recorded after its test's window closed still counts for that test, like a late tool call: it goes in the sums, and the test's budget is checked again, so a run within it can now fail. Raise `agent.settle_time` if that happens.

## `hello`: the writer started

| Field | Type | |
|---|---|---|
| `sdk` | string | The writer and its version, like `"highbar-python/0.1.0"`. |
| `pid` | integer | The writer's process id. |
| `tools` | list of strings | Optional: the tools it records, as in a `tools` record. |

```json
{"v": 1, "type": "hello", "ts": 1792983750.122, "sdk": "my-agent/1.0", "pid": 94015, "tools": ["get_order", "cancel_order"]}
```

Write it once, when the agent starts. When highbar starts the agent and a test checks tool calls, highbar waits up to 2 seconds after the agent is ready for a `hello` in the new file; without one, the run stops with exit code 2. Records of other types do not count: a `hello` says the writer started, before any message arrives. Against an attached agent, highbar checks that the file exists before it sends the first message.

## `tools`: the tools it records

| Field | Type | |
|---|---|---|
| `names` | list of strings | Required: tool names, as they appear in `fn`. |

```json
{"v": 1, "type": "tools", "ts": 1792983750.124, "names": ["cancel_order", "get_order"]}
```

A writer may write several. highbar joins those written since the writer's last `hello`, since earlier ones are another process's, and adds every name it sees in a `tool` record during the run. The Python SDK writes one each time a tool is decorated or declared, since its `hello` comes first. A name in a spec's `forbid` or `function_calls` that is in none of them makes the test an `error`: a guardrail on a tool the agent never records could not fail. A writer that declares nothing gets a warning instead, and older versions of highbar ignore the record.

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


def turn(test: str | None, usage: dict | None = None) -> None:
    """One message handled: before the reply goes out, also when the handling failed."""
    _write({"type": "turn", "usage": usage}, test)
```

The agent calls `hello(TOOLS)` before it listens, with the tools it records, `tool()` for each call, and `turn()` for each message, with the `X-Highbar-Test` header it received and the message's `usage`.

## Version 0

A line without `v` (or with `"v": 0`) is read as a v0 tool call: `fn`, `args`, `result`, `error`, `started_at` and `ended_at`, with every other top-level field (such as `conversation_id`) as its `context`. Version 0 has no `turn` records, so in attach mode its tests can check replies and calls, but a guardrail that expects no call needs the emitter to add a v1 `turn` line per message. Version 0 has no `hello` either: if the emitter creates the file only with its first call, create it empty before running highbar against the agent.
