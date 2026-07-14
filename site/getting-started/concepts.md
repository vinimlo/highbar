# Core Concepts

## Glossary

### Spec

A `.gauntlet.yaml` file that contains the full definition of a test run: agent configuration and one or more test suites. Each spec targets a single agent.

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"
  startup_timeout: 15000   # ms, default: 15000
  settle_time: 500          # ms, default: 500

suites:
  - name: ...
    tests: [...]
```

### Suite

A named group of related tests. Use suites to organize tests by feature, concern, or risk area.

```yaml
suites:
  - name: Order Management
    tests: [...]
  - name: Guardrails
    tests: [...]
```

### Test

A single behavioral assertion. A test can be **single-turn** (one trigger and one set of expectations) or **multi-turn** (a sequence of turns that share conversational state).

Single-turn test:

```yaml
- trigger: "Where is my order #12345?"
  expect:
    function_calls:
      - name: search_orders
        params:
          order_id: "12345"
    response:
      contains: "shipped"
```

Multi-turn test:

```yaml
- turns:
    - trigger: "Create an order for 3 widgets"
      expect:
        function_calls:
          - name: create_order
            params:
              item: "widgets"
              quantity: 3
    - trigger: "Now cancel that order"
      expect:
        function_calls:
          - name: cancel_order
```

### Turn

One exchange in a multi-turn test. Each turn has its own trigger and expectations. Turns execute sequentially against the same agent session, so conversational state carries forward.

### Trigger

The message sent to the agent's HTTP endpoint. Gauntlet sends it as a POST request with a JSON body:

```json
{"message": "Where is my order #12345?"}
```

### Expect

The assertions to evaluate after a trigger. Two assertion types are supported:

- **`function_calls`** — assert which tools were called, with what parameters, and optionally what they returned. Uses **subset matching**: you only need to specify the parameters you care about.
- **`response`** — assert properties of the agent's text response: `contains`, `not_contains`, or `matches` (regex).

```yaml
expect:
  function_calls:
    - name: search_orders
      params:
        order_id: "12345"
      result:
        status: "shipped"
  response:
    contains: "shipped"
    not_contains: "error"
```

### Trace

An atomic record capturing a single tool invocation. Each trace contains:

| Field | Description |
|-------|-------------|
| `id` | SHA-256 fingerprint (first 16 hex chars) derived from function name, arguments, and a random nonce |
| `seq` | Auto-incrementing sequence number for ordering |
| `fn` | The function name (e.g., `search_orders`) |
| `args` | The bound arguments as a dictionary |
| `result` | The return value |
| `started_at` | Unix timestamp when the call started |
| `ended_at` | Unix timestamp when the call completed |
| `error` | Error string, if the call raised an exception |

### Trace Box

The JSONL record written by `@gt.tool` to disk. Each line in the trace file is one JSON object representing a single tool invocation. The trace file path is controlled by the `GAUNTLET_TRACES_URL` environment variable, which Gauntlet injects automatically when starting the agent subprocess.

Example trace box (one line in the JSONL file):

```json
{"id": "a1b2c3d4e5f6a7b8", "seq": 0, "fn": "search_orders", "args": {"order_id": "12345"}, "result": {"order_id": "12345", "status": "shipped"}, "started_at": 1710000000.123, "ended_at": 1710000000.456}
```

## Architecture

```mermaid
graph LR
    A[Spec YAML] --> B[Parser]
    B --> C[Runner]
    C --> D[Assertions]
    D --> E[Reporter]
    C --> F[SubprocessAdapter]
    F -->|starts| G[Agent]
    G -->|"@gt.tool writes"| H[JSONL Traces]
    H --> I[FileTraceCollector]
    I -->|reads| D
```

## Execution Flow

Here is what happens when you run `gauntlet run specs/my-agent.gauntlet.yaml`:

1. **Parse** — The Parser reads the YAML spec file, validates the schema, and produces domain model objects (`Spec`, `Suite`, `Test`, `Expect`).

2. **Start Agent** — The `SubprocessAdapter` launches the agent command (e.g., `python agent.py`) as a child process. It injects the `GAUNTLET_TRACES_URL` environment variable pointing to a temporary JSONL file.

3. **Health Check** — If a `health` URL is configured, Gauntlet polls it with GET requests until it receives a successful response, up to `startup_timeout` milliseconds.

4. **Send Trigger** — For each test, the `HTTPClient` sends a POST request to the agent's `endpoint` with the trigger message.

5. **Settle** — Gauntlet waits for `settle_time` milliseconds after receiving the response. This gives the agent time to complete any asynchronous tool calls that happen after the HTTP response is sent.

6. **Collect Traces** — The `FileTraceCollector` reads the JSONL trace file and parses each line into a `Trace` object. It returns all traces written since the last collection.

7. **Assert** — The assertion engines (`FunctionCallsAssertion`, `ResponseAssertion`) compare the collected traces and response against the expected values defined in the spec. Function call assertions use subset matching on parameters — you only need to assert the fields you care about.

8. **Report** — The `TerminalReporter` outputs results to the terminal using rich formatting. Passing tests show a green checkmark, failing tests show a red cross with details about what went wrong.

9. **Cleanup** — After all tests complete, the agent subprocess is terminated and the temporary trace file is cleaned up. The CLI exits with code 0 if all tests passed, or code 1 if any test failed.

## Key Design Decisions

**Test the agent, not the LLM.** Gauntlet does not evaluate prompt quality or model accuracy. It asserts that given an input, the agent calls the right tools with the right parameters and produces the right output.

**Subset matching by default.** When asserting function call parameters, you only specify the fields that matter. If the tool receives additional parameters, the assertion still passes. This keeps specs resilient to implementation changes.

**Zero overhead in production.** The `@gt.tool` decorator checks for the `GAUNTLET_TRACES_URL` environment variable at initialization time. When the variable is not set, the decorator returns the original function unwrapped — no trace writing, no performance impact, no extra dependencies with the `[sdk]` install.

**YAML over code.** Specs are declarative YAML files, not Python test code. This makes them readable by anyone on the team, diffable in pull requests, and easy to generate from documentation or conversation logs.

## Next Steps

- [Quickstart](/getting-started/quickstart) — build and test your first agent in 5 minutes
- [Installation](/getting-started/installation) — install Gauntlet via pip or uv
