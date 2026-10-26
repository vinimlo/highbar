# Core concepts

## Specs, suites and tests

A spec is a `.highbar.yaml` file. It names the agent under test and groups its tests in suites. `highbar run` with no argument runs every spec under `specs/`.

A test is a message (`trigger`) and what must follow from it (`expect`):

```yaml test
name: looks the order up
trigger: "Where is order 1042?"
expect:
  function_calls:
    - name: get_order
      params: {order_id: "1042"}
  response:
    contains: "1042"
```

`expect` can check the tool calls (`function_calls`, with `ordered`, `exclusive` and `error`), tools that must not be called (`forbid`), and the reply (`response`). A test that checks nothing is a spec error, and so is any key highbar does not know: a typo never turns into a check that is silently skipped. [Writing specs](/guides/writing-specs) has the whole vocabulary.

A conversation is a test with `turns`, each with its own trigger and expectations. See [multi-turn tests](/guides/multi-turn).

## The agent: started or attached

With `start`, highbar starts the agent itself, waits for `health` to answer, runs the tests and stops it. The agent's output goes to a log file under `.highbar/logs/`.

```yaml agent
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:8731/chat"
  health: "http://127.0.0.1:8731/health"
```

Without `start`, highbar attaches to an agent that is already running and never stops it. The agent writes its trace file where `traces` says:

```yaml agent
agent:
  endpoint: "https://staging.example.com/api/chat"
  traces: "/var/log/support-agent/traces.jsonl"
```

By default highbar sends `{"message": "..."}` and reads the reply from the first of the keys `response`, `message`, `content`, `text` and `reply`, or takes the body itself when it is plain text. Both sides are configurable, and a session hook can open a fresh conversation for each test: see [attach mode and sessions](/guides/attach-mode).

## Traces

The agent records what it does in a trace file, one JSON line per event:

- a `tool` record for each tool call, with its arguments, its result, and its `error` if it failed;
- a `turn` record for each message it handled;
- a `hello` record when the SDK starts writing.

The Python SDK writes these for you (`@hb.tool`, `hb.record()`, and `highbar.asgi.Middleware` for the turns). Any other language can write them directly: the format is the [trace contract](/reference/trace-contract).

## Which calls belong to a test

Each execution of a test gets its own id, sent in the `X-Highbar-Test` header. `highbar.asgi.Middleware` (or `hb.turn()`) puts it in the `context` of every record written while that request is handled, and highbar keeps only the records whose context matches, so a slow call from the previous test cannot count for this one. Records without a test id count for the test whose window they fall in, and highbar warns once that a slow call can land in the next test.

When the test id cannot travel to the agent's tools, `agent.correlate` names another key, such as a `conversation_id` opened by the session hook.

An empty trace is never taken as proof. When highbar starts the agent, it waits for the writer's `hello` before any test that checks tool calls runs, and stops with exit code 2 without it: that is how a stale server on the port, or an SDK that never started, is caught. When records carry test ids, and always in attach mode, a test that checks tool calls also needs a record of its own; without one it is an `error` that says what was read, never a pass.

## Three results

- **passed**: the agent did what the spec says, and highbar saw it.
- **failed**: the agent did something else.
- **error**: highbar could not verify it.

`highbar run` exits with 0 when every test passed, 1 when any failed or errored, and 2 when nothing could run: an invalid spec, an agent that did not start, a port already in use.

## Runs

An agent built on an LLM can pass once by chance. `runs` sends the message several times, each as its own execution, and `min_pass` says how many must pass (every run, by default):

```yaml test
trigger: "I want to cancel my order"
runs: 5
min_pass: 4
expect:
  function_calls:
    - name: cancel_order
```

`--runs N` on the command line sets the default for every test that does not set its own. See [repetition and CI gates](/guides/repetition).
