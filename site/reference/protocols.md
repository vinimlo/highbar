# Extension Protocols

Highbar uses Python `Protocol` classes (structural subtyping) for its extension points. Any class that implements the required methods is compatible -- no inheritance or registration needed.

All protocols are defined in `highbar.domain.protocols`.

---

## `Assertion`

Evaluates captured traces and response text against expected outcomes.

```python
class Assertion(Protocol):
    def evaluate(
        self, traces: list[Trace], response: str, expect: Expect
    ) -> list[AssertionResult]: ...
```

### Method: `evaluate`

| Parameter | Type | Description |
|-----------|------|-------------|
| `traces` | `list[Trace]` | Trace boxes collected during the test |
| `response` | `str` | The agent's HTTP response text |
| `expect` | `Expect` | The expected outcomes from the spec |
| **Returns** | `list[AssertionResult]` | One result per assertion check performed |

**Built-in implementations:**

| Class | Module | Description |
|-------|--------|-------------|
| `FunctionCallsAssertion` | `highbar.assertions.function_calls` | Validates tool invocations: name, params (subset match), result (subset match), and empty-list guardrails |
| `ResponseAssertion` | `highbar.assertions.response` | Validates response text: `contains`, `not_contains`, `matches` (regex) |

---

## `Reporter`

Formats and outputs test results. Called by the `TestRunner` at specific points during execution.

```python
class Reporter(Protocol):
    def on_suite_start(self, suite: Suite) -> None: ...
    def on_test_result(self, test_name: str, result: TestResult) -> None: ...
    def on_run_end(self, summary: RunSummary) -> None: ...
```

### Methods

#### `on_suite_start(suite)`

Called before the first test in each suite. Use this to print suite headers or initialize per-suite state.

| Parameter | Type | Description |
|-----------|------|-------------|
| `suite` | `Suite` | The suite about to be executed |

#### `on_test_result(test_name, result)`

Called after each test completes (pass or fail).

| Parameter | Type | Description |
|-----------|------|-------------|
| `test_name` | `str` | The trigger text or multi-turn description |
| `result` | `TestResult` | Contains `passed`, `errors`, `duration_ms`, `traces` |

#### `on_run_end(summary)`

Called after all suites have completed.

| Parameter | Type | Description |
|-----------|------|-------------|
| `summary` | `RunSummary` | Contains `total`, `passed`, `failed`, `duration_ms` |

**Built-in implementation:**

| Class | Module | Description |
|-------|--------|-------------|
| `TerminalReporter` | `highbar.reporters.terminal` | Rich-formatted terminal output with colors and timing |

---

## `AgentAdapter`

Manages the agent process lifecycle.

```python
class AgentAdapter(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

### Methods

#### `start()`

Starts the agent process and waits until it is ready to receive requests (e.g., health check passes).

#### `stop()`

Shuts down the agent process. Implementations should handle graceful shutdown with a fallback to forced termination.

**Built-in implementation:**

| Class | Module | Description |
|-------|--------|-------------|
| `SubprocessAdapter` | `highbar.adapters.subprocess_adapter` | Starts agent as a subprocess, polls health endpoint, terminate/kill on stop |

---

## `TraceCollector`

Reads captured trace boxes from a trace store. Uses a checkpoint mechanism to read only traces added during a specific test.

```python
class TraceCollector(Protocol):
    def checkpoint(self) -> int: ...
    def collect(self, since: int) -> list[Trace]: ...
```

### Methods

#### `checkpoint() -> int`

Returns the current position in the trace store. Call this **before** sending a trigger to mark where new traces should be read from.

#### `collect(since) -> list[Trace]`

Reads all traces added after the given checkpoint position.

| Parameter | Type | Description |
|-----------|------|-------------|
| `since` | `int` | Checkpoint value returned by a prior `checkpoint()` call |
| **Returns** | `list[Trace]` | Trace boxes written since the checkpoint |

**Built-in implementation:**

| Class | Module | Description |
|-------|--------|-------------|
| `FileTraceCollector` | `highbar.adapters.file_collector` | Reads JSONL files; checkpoint returns line count; collect reads lines added since checkpoint |
