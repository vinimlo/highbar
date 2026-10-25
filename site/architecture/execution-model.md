# Execution Model

This page describes the step-by-step flow of a Highbar test run, from spec parsing to final summary.

## Full Execution Flow

### 1. Parse Spec

The spec YAML file is parsed into domain models: `Spec`, `AgentConfig`, `Suite`, `Test`, `Expect`, `Turn`, `FunctionCallExpect`, and `ResponseExpect`. The parser validates the structure and reports errors before any execution begins.

### 2. Create Trace File

A temporary JSONL file is created (e.g., `/tmp/highbar-traces-abc123.jsonl`). This file serves as the communication channel between the agent's `@hb.tool` decorator and Highbar's trace collector.

### 3. Start Agent Subprocess

The agent is launched as a subprocess using the `start` command from the spec's `agent` block. The environment variable `HIGHBAR_TRACES_URL` is injected, pointing to the temporary trace file.

### 4. Wait for Health Check

If a `health` URL is configured, Highbar polls it every **200ms** until it receives an HTTP 200 response. If the health check does not pass within `startup_timeout` (default: 15,000ms), Highbar stops the agent and raises a `TimeoutError`.

If no `health` URL is configured, Highbar proceeds immediately to test execution.

### 5. Execute Tests

For each suite, and for each test within the suite:

#### a. Record Checkpoint

Highbar calls `collector.checkpoint()`, which returns the current line count of the JSONL trace file. This marks the boundary -- any traces written after this point belong to the current test.

#### b. Resolve Templates

For multi-turn tests, trigger strings may contain template expressions (e.g., `previous.function_calls[0].result.order_id` wrapped in double curly braces). These are resolved against the accumulated traces from all preceding turns in the same test. For single-turn tests or first turns, no resolution is needed.

#### c. Send Trigger

Highbar sends an HTTP POST request to the agent's `endpoint`:

```json
{"message": "Where is my order #12345?"}
```

The agent processes the request, potentially calling `@hb.tool`-decorated functions (which write trace boxes to the JSONL file), and returns an HTTP response.

#### d. Receive Response

The response JSON is parsed. Highbar checks for common response fields (`response`, `message`, `content`, `text`, `reply`) and extracts the text value.

#### e. Wait Settle Time

Highbar waits for `settle_time` (default: 500ms) after receiving the response. This gives the agent time to finish writing any remaining trace records -- for example, if tool calls happen asynchronously or in a post-processing step.

#### f. Read Traces

Highbar calls `collector.collect(since=checkpoint)`, which reads all JSONL lines added after the checkpoint. Each line is parsed into a `Trace` domain object.

#### g. Run Assertions

All registered assertion implementations evaluate the collected traces and response text against the `expect` block. Each assertion returns a list of `AssertionResult` objects. Any result with `passed=False` is recorded as an error.

#### h. Report Result

The `Reporter` is called with the test name and `TestResult` (pass/fail, errors, duration, traces).

### 6. Stop Agent

After all tests complete (or if an error occurs), Highbar shuts down the agent:

1. Sends `SIGTERM` to the process.
2. Waits up to 5 seconds for graceful shutdown.
3. Sends `SIGKILL` if the process has not exited.

The shutdown happens in a `finally` block, ensuring the agent is always stopped even if tests raise exceptions.

### 7. Report Summary

The `Reporter` receives the `RunSummary` with total, passed, failed counts and overall duration. The CLI exits with code 0 if all tests passed, or code 1 if any test failed.

## Why Checkpoint + Settle Time Works

Traditional test frameworks correlate requests to responses using request IDs or correlation tokens. This requires middleware in the agent to thread the ID through every tool call.

Highbar takes a simpler approach:

1. **Before** sending a trigger, record the current line count in the trace file.
2. **After** receiving the response (plus settle time), read everything new.

This works because:

- Tests run **sequentially** -- there is only one trigger in flight at a time.
- The trace file is **append-only** -- new traces are always added at the end.
- The **settle time** provides a buffer for traces that are written after the HTTP response returns (e.g., async operations, cleanup handlers).

The result: the `@hb.tool` decorator is the **only** instrumentation needed. No request ID, no middleware, no correlation tokens. The decorator itself does not even know about tests or Highbar's execution model -- it simply writes trace boxes.

## Multi-Turn Execution

Multi-turn tests execute as a sequence of turns within a single test. Traces accumulate across turns:

```
Turn 1:
  checkpoint → send trigger → receive response → wait → collect traces
  accumulated_traces = [trace_A, trace_B]

Turn 2:
  resolve &#123;&#123;previous.function_calls[0].result.order_id&#125;&#125; using accumulated_traces
  checkpoint → send trigger → receive response → wait → collect traces
  accumulated_traces = [trace_A, trace_B, trace_C]
```

If any turn fails its assertions, the remaining turns are skipped and the entire test is marked as failed. The error messages and traces from all executed turns are included in the `TestResult`.

## Timing Diagram

```
Time →

Highbar:    [parse] [start agent] [health poll...] [checkpoint] [POST trigger]     [wait settle] [read traces] [assert]
                                                                      │                    │
Agent:                [starting...]  [ready]                     [process request]    [done]
                                                                      │
@hb.tool:                                                        [write trace A]
                                                                 [write trace B]
```
