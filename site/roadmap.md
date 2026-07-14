# Roadmap

Features planned for future releases of Gauntlet. These are not in the current MVP.

## Assertions

### Semantic Variations

```yaml
tests:
  - trigger: "Where is my order #12345?"
    variations: 5
    expect:
      function_calls:
        - name: search_orders
```

Auto-generate paraphrases of the trigger message (e.g., "Track my order #12345", "Can you look up order 12345?", "I need the status of order #12345"). The test passes only if all variations produce the same expected behavior. This validates that the agent's tool selection is robust to natural language variation, not just tuned to specific phrasings.

### `max_duration`

```yaml
expect:
  function_calls:
    - name: search_orders
      max_duration: 2000  # milliseconds
```

Assert that a tool call completes within a time limit. Useful for catching performance regressions in tool implementations or detecting runaway API calls.

### `call_count`

```yaml
expect:
  function_calls:
    - name: search_orders
      call_count: 1
```

Assert the exact number of times a function is called. Prevents agents from making redundant or duplicate tool calls that waste resources.

### `call_order: strict`

```yaml
expect:
  call_order: strict
  function_calls:
    - name: search_orders
    - name: get_shipping_details
```

Enforce that function calls happen in exactly the specified sequence. Currently, Gauntlet only checks that all expected calls are present. Strict ordering adds sequence verification for workflows where order matters (e.g., authenticate before querying).

### `result_not_contains`

```yaml
expect:
  function_calls:
    - name: get_customer_details
      result_not_contains: "[0-9]{3}-[0-9]{2}-[0-9]{4}"
```

Regex assertion on tool return values. Catches cases where the tool itself returns PII that should have been filtered before reaching the agent's response generation.

## Configuration

### Test-Level Config

```yaml
tests:
  - trigger: "Process this large batch"
    config:
      timeout: 60000
      settle_time: 5000
      retries: 2
    expect:
      function_calls:
        - name: batch_process
```

Override timeout, settle time, and retry count per test. Currently, these values are set globally in the `agent` block.

## Reporters

### HTML Reporter

Generate a self-contained HTML file with interactive test results -- expandable test details, trace inspection, timing charts, and filterable pass/fail views. Suitable for sharing test results with non-technical stakeholders or archiving in CI artifacts.

### JSON Reporter

Machine-readable JSON output for integration with CI/CD pipelines, dashboards, and monitoring tools. Each test result includes full trace data, timing, and assertion details.

```json
{
  "total": 12,
  "passed": 11,
  "failed": 1,
  "duration_ms": 4500,
  "suites": [
    {
      "name": "Order Lookup",
      "tests": [
        {
          "trigger": "Where is my order #12345?",
          "passed": true,
          "duration_ms": 120,
          "traces": [...]
        }
      ]
    }
  ]
}
```

## Transport

### HTTP Sidecar

An HTTP-based trace transport for remote agents that cannot share a local filesystem with the test runner. The sidecar runs as a lightweight HTTP server alongside the test runner, and the agent's `@gt.tool` decorator posts traces to it over HTTP instead of writing to a file.

```yaml
agent:
  start: "docker run -e GAUNTLET_TRACES_URL=http://host:9090/traces my-agent"
  endpoint: "http://localhost:8080/chat"
  trace_transport: http
  trace_port: 9090
```

This enables testing agents running in Docker containers, Kubernetes pods, or remote cloud environments.

## Explicitly Not Planned

### Mocks

Mocking tool functions or LLM responses was considered and rejected. Gauntlet tests real agent behavior end-to-end. Mocking any component in the chain (tools, LLM, orchestration) defeats the purpose of the framework.

If your tools have side effects, configure a test environment with sandboxed services -- the same approach used for integration testing in web applications.

### LLM Evaluation Metrics

Gauntlet does not plan to add quality scoring, hallucination detection, or faithfulness metrics. These are well-served by existing tools like Promptfoo and DeepEval. Gauntlet focuses on behavior assertions, not output quality measurement.
