# Trace contract v1

The trace file is how an agent tells highbar what it did. The Python SDK writes it; an agent in any other language writes it directly. The format is versioned so that both sides can change without breaking each other.

## The file

- One JSON object per line (JSON Lines), UTF-8, each line ending in a newline.
- The agent appends; highbar reads from where it stopped last time. At the start of a run it also scans the file once, for the tools declared since the last `hello`. A line not yet ended by a newline is left for the next read, so a writer may be in the middle of one.
- A file that becomes smaller than where highbar stopped was recreated (a restarted agent, a rotated log), and is read from the start.
- A line that is not valid JSON, a `tool` record without `fn`, or `args` or `context` that are not objects, is an error for the test that read it, naming the line. It is never skipped: it may be the call the test is about. It may also be the late usage of the test before, so that test is an error too if it passed and has a budget; read after the last test, it makes that test an error if it passed, budget or not. The other lines read with it still count: their usage is in the sums, in `--max-cost` and in budgets, and their late or untagged records count as they would in any read. A timestamp that is not a number falls back to `ts`.

## Common fields

| Field | Type | |
|---|---|---|
| `v` | integer | `1`. A version newer than highbar reads is an error that says to upgrade highbar. |
| `type` | string | Required: `"tool"`, `"turn"`, `"hello"`, `"tools"` or `"bye"`. A line with another type, or none, is skipped, so emitters can add new types; a tool call written without `type` is lost. |
| `ts` | number | When the record was written, in seconds since the epoch. |
| `context` | object | Optional. What links the record to a test: `test` (the `X-Highbar-Test` header) and any key `agent.correlate` uses, like `conversation_id`. |

Unknown fields are ignored.

## `tool`: one tool call

| Field | Type | |
|---|---|---|
| `fn` | string | The tool's name. Required. |
| `args` | object | The arguments, by name. |
| `result` | any JSON | What the tool returned. highbar never reads it to decide whether the call failed. Left out, the return was not seen: a `returns` check on the call is an `error`, while `"result": null` is a return of null. |
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

A value that cannot be read counts as missing, and the line is still read: a `usage` that is not an object, a negative count, text where a number goes, `NaN`. So does `input_tokens` below `cached_input_tokens`, which contradicts the table above: highbar warns and works out no cost from that record. A budget on a missing value is an `error`, never a pass.

When several `turn` records of one message have `usage`, such as `highbar.asgi.Middleware` next to `hb.turn()`, they are added up, because all of it was spent. A value they disagree on, like the model, is unknown in the turn's `usage` in the report; the sums and budgets read each record, so a value one of them knows is never lost. A `usage` that cannot be read at all, next to one that can, counts as a record with nothing known. A `turn` without `usage`, or with an empty one like `null` or `{}`, counts for nothing.

A `turn` with `usage` but no test context, next to records that have one, counts for no test, as an untagged tool call does. It is left out of the sums, which say `complete: false`, and a test whose budget holds only without it is an `error`. The one thing that counts it is `--max-cost`, and only for an agent highbar started, as the [CLI reference](/reference/cli#max-cost) explains. A `hb.turn()` in a thread the agent starts itself needs `contextvars.copy_context()` to keep the tag.

When no record of a window has a test context, an agent highbar started gets its `turn` counted for the test whose window it falls in, and highbar warns once. That usage may have been spent by the test before, so a test whose budget holds only without the usage of the next test's windows is an `error`, whether or not anything was late, and with an attached agent too. A test that had no window read, because its message failed to send or its session did not open, is passed over: the test before it answers instead. The sums and `--max-cost` count that usage once, where it was read. Tagging the records removes the doubt.

A `cost` from the writer always stands. Without one, highbar works it out from `src/highbar/engine/prices.json`, a table of list prices in USD per token, in the format of [OpenRouter's model list](https://openrouter.ai/api/v1/models). The price of cached tokens applies to `cached_input_tokens`. It needs `model`, `input_tokens` and `output_tokens`; it uses the table's currency, whatever `currency` says; and it finds the model by its exact name, ignoring case and with `.` the same as `-`, so a dated snapshot like `gpt-4o-2024-05-13` is not its alias. A model without a price leaves the cost missing, with a warning. `provider` is required when two providers in the table use the name. An agent on prices of its own, or on a model outside the table, writes `cost` and `currency`.

Write `turn` before the reply goes out. A `turn` with `usage` recorded after its test's window closed still counts for that test, like a late tool call: it goes in the sums, and the test's budget is checked again, so a run within it can now fail. Raise `agent.settle_time` if that happens.

## `llm`: one call to the model

One per call, a failed one or a fallback included. Every field is optional; older versions of highbar ignore the record, and the contract stays at v1. `hb.llm(...)` writes it from Python.

| Field | Type | |
|---|---|---|
| `model` | string | The model the call went to. |
| `provider` | string | Its provider. |
| `attempt` | integer | 1 for the first try, more for a retry. |
| `fallback_from` | string | The model this call stands in for, when it is a fallback. |
| `roles` | list of strings | The role of each message sent, in order: `["system", "user", "system"]`. Anything else in `roles` makes the line unreadable. |
| `input_tokens`, `output_tokens`, `cache_tokens` | integers | What the call used. Not added to `usage` or to any budget: the `turn`'s `usage` stays the one count, so nothing counts twice. |
| `latency_ms` | number | How long the call took. |
| `error` | string | Why the call failed. Absent, `null`, `false` or `""`: it did not. |
| `source` | string | Who wrote the record: `"proxy"` for highbar's LLM proxy. Absent: the agent. |
| `read` | boolean | Whether the writer read the call's usage whole. `false` says it saw the call and its count is not whole; absent, the writer did not say. Anything but `true`, `false` or `null` makes the line unreadable. |
| `unread` | string | Why the usage was not read, when `read` is `false`. |
| `cost`, `currency` | number, string | What the provider said the call cost, such as OpenRouter's `usage.cost` (in `USD`). |

It goes to a test by its `context`, like a `tool` record, and it is proof that the agent handled the message. One recorded after its test's window closed still counts for that test, which is checked again. [`expect.llm`](/guides/assertions#calls-to-the-model-llm) checks these records.

```json
{"v": 1, "type": "llm", "ts": 1792983750.101, "model": "deepseek-flash", "provider": "openrouter", "attempt": 2, "fallback_from": "gpt-4o-mini", "roles": ["system", "user", "system"], "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

### Records from highbar's LLM proxy

highbar's LLM proxy sits between the agent and its model provider, passes every request and reply through unchanged, and writes one `llm` record per call with `"source": "proxy"`. It reads the usage the provider reports: OpenAI-compatible `chat/completions` and `responses` (OpenRouter, DeepSeek and local servers included) and Anthropic `messages`, streamed or not. `input_tokens` is the whole input, cached tokens included, as in a `turn`'s `usage`: for Anthropic it adds `cache_read_input_tokens` and `cache_creation_input_tokens` to `input_tokens`, and `cache_tokens` is `cache_read_input_tokens`. `model` is the one the reply names, or the one the request asked for when the reply names none. The test context is the request's `X-Highbar-Test` header, when it has one.

```json
{"v": 1, "type": "llm", "ts": 1792983750.301, "source": "proxy", "model": "gpt-4o-mini-2024-07-18", "provider": "openai", "input_tokens": 1000, "output_tokens": 200, "cache_tokens": 800, "read": true, "latency_ms": 812.4, "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
```

A call whose usage it could not read is recorded all the same, with `"read": false` and the reason in `unread`. That is a stream that ended without usage, a reply without usage (an error status, for one), a path or a provider whose format the proxy does not read (passed through as it is), an upstream it could not reach, and a reply cut off before its end. Its token fields are left out, or hold only what was read: an unread call is a count that is not whole, never zero tokens.

```json
{"v": 1, "type": "llm", "ts": 1792983751.020, "source": "proxy", "model": "gpt-4o-mini-2024-07-18", "provider": "openai", "read": false, "unread": "the stream ended without usage", "latency_ms": 1301.7}
```

For an OpenAI chat stream that does not set `stream_options.include_usage`, the proxy sets it to `true`, and asks for the stream uncompressed. It reads the usage from the chunk the upstream adds for it, the one with `"choices": []`, and does not forward that chunk, so the agent gets the stream it would get directly, `data: [DONE]` included. A stream that sets `include_usage` itself gets that chunk as usual, and one that sets it to `false` is sent as it is and recorded unread. The proxy never logs a header, a URL or a body, and never writes a key or a body to the trace.

## `retrieval`: what entered the prompt

One per message: the documents the agent put in the prompt, and how. Every field is optional, but each document needs an `id` or a `title`, and its `chunks` must be whole numbers: anything else makes the line unreadable. `hb.retrieval(...)` writes it from Python.

| Field | Type | |
|---|---|---|
| `items` | list | The documents: `{id, title, source, chunks, tokens}`. `source` is `retrieved`, `expanded` (brought in whole) or `sticky` (kept from an earlier turn); `chunks` are the indexes of its parts that went in, in the order they went in. |
| `query`, `search_query`, `strategy`, `skipped_reason` | strings | What was searched, how, and why nothing was, when nothing was. |
| `expired` | list | Documents that left the prompt: `{id, reason}`. |
| `tokens` | integer | The tokens the documents took. Without it, the documents' own `tokens` add up, when every one has them. |

It goes to a test by its `context`, like a `tool` record. It is proof that the agent handled the message, and it is not a tool call: `function_calls: []` and `exclusive` do not see it. One recorded after its test's window closed still counts for that test, which is checked again. [`expect.retrieval`](/guides/assertions#what-entered-the-prompt-retrieval) checks these records.

```json
{"v": 1, "type": "retrieval", "ts": 1792983750.090, "query": "and the deadline?", "items": [{"id": "tr-7", "title": "Warranty", "source": "expanded", "chunks": [0, 1, 2], "tokens": 900}], "context": {"test": "9784235d06704e5da37c3230a9e99b34"}}
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

Write it once, when the agent starts. When highbar starts the agent and a test checks tool calls, highbar waits up to 2 seconds after the agent is ready for a `hello` in the new file; without one, the run stops with exit code 2. Records of other types do not count: a `hello` says the writer started, before any message arrives. Against an attached agent, highbar checks that the file exists before it sends the first message.

## `tools`: the tools it records

| Field | Type | |
|---|---|---|
| `names` | list of strings | Required: tool names, as they appear in `fn`. |

```json
{"v": 1, "type": "tools", "ts": 1792983750.124, "names": ["cancel_order", "get_order"]}
```

A writer may write several. highbar joins those written since the writer's last `hello`, since earlier ones are another process's, and adds every name it sees in a `tool` record during the run. The Python SDK writes one each time a tool is decorated or declared, since its `hello` comes first. A name in a spec's `forbid` or `function_calls` that is in none of them makes the test an `error`: a guardrail on a tool the agent never records could not fail. A writer that declares nothing gets a warning instead, and older versions of highbar ignore the record.

## `bye`: the writer ended

| Field | Type | |
|---|---|---|
| `pid` | integer | The process that ended: the `pid` of its `hello`. |

```json
{"v": 1, "type": "bye", "ts": 1792987350.411, "pid": 94015}
```

Write it when the process exits. A `bye` with the `pid` of the file's last `hello` says that process ended, so the tools it declared no longer count: an old build's file must not vouch for a tool the code running now no longer records. A `bye` from another process, a forked child's, ends nothing. The Python SDK writes one when Python itself ends the process: the end of its main module, `sys.exit`, or Ctrl-C. A process stopped by SIGTERM, which is how `kill`, `docker stop` and `docker compose down` stop it, writes none, and neither does one stopped by SIGKILL, `os._exit` or a crash. Older versions of highbar ignore the record.

## Matching records to a test

Each test execution sends its id in the `X-Highbar-Test` header (and, when highbar reads [OpenTelemetry spans](#opentelemetry-spans-otlp), as the trace id of a `traceparent`). A writer copies it into `context.test` of every record made while handling that request. highbar keeps the records whose `context` matches `agent.correlate` (by default, `test`); numbers and text that read the same match, so `42` matches `"42"`.

## OpenTelemetry spans (OTLP)

With `agent.traces.format: otlp`, the file holds OpenTelemetry spans instead: one OTLP/JSON `ExportTraceServiceRequest` per line (`resourceSpans[].scopeSpans[].spans[]`), as the Collector's `file` exporter writes it. The guide is [OpenTelemetry traces](/guides/opentelemetry). It is read by offset like this contract, and a line that is not valid JSON, or a tool or agent span that cannot be read, is an error for the test that read it, naming the line. Lines with logs or metrics and spans of other kinds are skipped.

The mapping is versioned in highbar's code (`OTLP_MAPPING_VERSION`, now `0`). It follows OpenTelemetry's GenAI semantic conventions as published in Development status, which may still change, plus OpenInference's span kinds. It reads only the attributes in this table, and the span's `traceId`, start and end times and status:

| Record | GenAI conventions | OpenInference |
|---|---|---|
| a `tool` record | a span with `gen_ai.operation.name` = `execute_tool` | `openinference.span.kind` = `TOOL` |
| `fn` | `gen_ai.tool.name`, else the span name after `execute_tool ` | `tool.name`, else the span name |
| `args` | `gen_ai.tool.call.arguments`, a JSON object (as text or as a map) | `input.value` |
| `result` | `gen_ai.tool.call.result`; text that is JSON is parsed | `output.value` |
| `error` | status `ERROR` (its message), or `error.type` | the same |
| `started_at`, `ended_at` | `startTimeUnixNano`, `endTimeUnixNano` | the same |
| a `turn` record | `gen_ai.operation.name` = `invoke_agent` | `openinference.span.kind` = `AGENT` |
| `response` | the text parts of the last assistant message in `gen_ai.output.messages` | `output.value` |
| `usage.input_tokens`, `output_tokens` | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `llm.token_count.prompt`, `llm.token_count.completion` |
| `usage.model` | `gen_ai.response.model`, else `gen_ai.request.model` | `llm.model_name` |
| `usage.provider` | `gen_ai.provider.name`, else `gen_ai.system` | `llm.provider` |
| `context.test` | the span's `traceId` | the same |

Each message highbar sends carries a W3C `traceparent` whose trace id is the test execution's id, so `context.test` is the test a span belongs to. A tool span without arguments was exported with content capture off: a `params` check on it is an `error`, and so is a `returns` check on one without a result. `chat` and `LLM` spans (the `llm` record) and `RETRIEVER` spans (the `retrieval` record) are not read in version 0, and nothing in an OTLP file declares tools.

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


def tool(fn, args, *, result=..., error=None, started_at, test=None) -> None:
    """One tool call. `error` says why it failed. No `result=`: the return was not seen."""
    record = {"fn": fn, "args": args, "error": error}
    record |= {} if result is ... else {"result": result}  # unseen is not "returned null"
    _write({"type": "tool", **record, "started_at": started_at, "ended_at": time.time()}, test)


def turn(test: str | None, usage: dict | None = None) -> None:
    """One message handled: before the reply goes out, also when the handling failed."""
    _write({"type": "turn", "usage": usage}, test)
```

The agent calls `hello(TOOLS)` before it listens, with the tools it records, `tool()` for each call, and `turn()` for each message, with the `X-Highbar-Test` header it received and the message's `usage`.

## Version 0

A line without `v` (or with `"v": 0`) is read as a v0 tool call: `fn`, `args`, `result`, `error`, `started_at` and `ended_at`, with every other top-level field (such as `conversation_id`) as its `context`. Version 0 has no `turn` records, so in attach mode its tests can check replies and calls, but a guardrail that expects no call needs the emitter to add a v1 `turn` line per message. Version 0 has no `hello` either: if the emitter creates the file only with its first call, create it empty before running highbar against the agent.
