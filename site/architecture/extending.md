# Extending Highbar

Highbar uses Python `Protocol` classes for its extension points. You can add custom assertions, reporters, and trace collectors without modifying any existing code -- just implement the required methods.

## Custom Assertions

Create a class that implements the `Assertion` protocol to add new validation logic.

### Example: Maximum Duration Assertion

This assertion fails if any tool invocation takes longer than a specified threshold:

```python
from highbar.domain.models import AssertionResult, Expect, Trace


class MaxDurationAssertion:
    """Fails if any tool call exceeds the maximum duration."""

    def __init__(self, max_ms: float = 5000):
        self.max_ms = max_ms

    def evaluate(
        self, traces: list[Trace], response: str, expect: Expect
    ) -> list[AssertionResult]:
        results = []
        for trace in traces:
            if trace.duration_ms > self.max_ms:
                results.append(
                    AssertionResult(
                        passed=False,
                        message=f"'{trace.fn}' took {trace.duration_ms:.0f}ms (max: {self.max_ms}ms)",
                    )
                )
        return results
```

### Example: Response Length Assertion

This assertion checks that the response is within a reasonable length range:

```python
from highbar.domain.models import AssertionResult, Expect, Trace


class ResponseLengthAssertion:
    """Fails if the response is too short or too long."""

    def __init__(self, min_chars: int = 10, max_chars: int = 5000):
        self.min_chars = min_chars
        self.max_chars = max_chars

    def evaluate(
        self, traces: list[Trace], response: str, expect: Expect
    ) -> list[AssertionResult]:
        results = []
        if len(response) < self.min_chars:
            results.append(
                AssertionResult(
                    passed=False,
                    message=f"Response too short: {len(response)} chars (min: {self.min_chars})",
                )
            )
        if len(response) > self.max_chars:
            results.append(
                AssertionResult(
                    passed=False,
                    message=f"Response too long: {len(response)} chars (max: {self.max_chars})",
                )
            )
        return results
```

## Custom Reporters

Create a class that implements the `Reporter` protocol to customize output formatting.

### Example: JSON Reporter

```python
import json
import sys

from highbar.domain.models import RunSummary, Suite, TestResult


class JSONReporter:
    """Outputs test results as JSON lines to stdout."""

    def on_suite_start(self, suite: Suite) -> None:
        print(json.dumps({"event": "suite_start", "name": suite.name}))

    def on_test_result(self, test_name: str, result: TestResult) -> None:
        print(
            json.dumps(
                {
                    "event": "test_result",
                    "test": test_name,
                    "passed": result.passed,
                    "errors": result.errors,
                    "duration_ms": result.duration_ms,
                }
            )
        )

    def on_run_end(self, summary: RunSummary) -> None:
        print(
            json.dumps(
                {
                    "event": "run_end",
                    "total": summary.total,
                    "passed": summary.passed,
                    "failed": summary.failed,
                    "duration_ms": summary.duration_ms,
                }
            )
        )
```

## Custom Trace Collectors

Create a class that implements the `TraceCollector` protocol to read traces from alternative sources.

### Example: HTTP Trace Collector

For agents running on remote machines where file access is not available:

```python
import httpx

from highbar.domain.models import Trace


class HTTPTraceCollector:
    """Collects traces from an HTTP endpoint instead of a file."""

    def __init__(self, base_url: str):
        self._base_url = base_url
        self._checkpoint_id: int = 0

    def checkpoint(self) -> int:
        resp = httpx.get(f"{self._base_url}/traces/count")
        self._checkpoint_id = resp.json()["count"]
        return self._checkpoint_id

    def collect(self, since: int) -> list[Trace]:
        resp = httpx.get(f"{self._base_url}/traces", params={"since": since})
        data = resp.json()
        return [
            Trace(
                id=t["id"],
                seq=t["seq"],
                fn=t["fn"],
                args=t["args"],
                result=t["result"],
                started_at=t["started_at"],
                ended_at=t["ended_at"],
                error=t.get("error"),
            )
            for t in data["traces"]
        ]
```

## Registering Custom Extensions

The composition root is `highbar/cli/main.py`. To use your custom implementations, modify the `run` command to include them in the `TestRunner` setup.

### Adding a Custom Assertion

```python
# In highbar/cli/main.py, inside the run() function:

from my_extensions import MaxDurationAssertion

runner = TestRunner(
    adapter=adapter,
    client=client,
    collector=collector,
    assertions=[
        FunctionCallsAssertion(),
        ResponseAssertion(),
        MaxDurationAssertion(max_ms=3000),  # Add your custom assertion
    ],
    reporter=reporter,
    settle_time=spec.agent.settle_time / 1000.0,
)
```

### Replacing the Reporter

```python
from my_extensions import JSONReporter

runner = TestRunner(
    adapter=adapter,
    client=client,
    collector=collector,
    assertions=[FunctionCallsAssertion(), ResponseAssertion()],
    reporter=JSONReporter(),  # Replace the terminal reporter
    settle_time=spec.agent.settle_time / 1000.0,
)
```

### Replacing the Trace Collector

```python
from my_extensions import HTTPTraceCollector

collector = HTTPTraceCollector(base_url="http://agent-host:9090")

runner = TestRunner(
    adapter=adapter,
    client=client,
    collector=collector,  # Use HTTP instead of file-based collection
    assertions=[FunctionCallsAssertion(), ResponseAssertion()],
    reporter=reporter,
    settle_time=spec.agent.settle_time / 1000.0,
)
```

## Guidelines for Extensions

- **Assertions** should return an empty list when their check is not applicable (e.g., a duration assertion returns `[]` when there are no traces).
- **Reporters** should handle all three lifecycle methods, even if some are no-ops.
- **Trace collectors** must ensure that `collect(since=checkpoint())` returns an empty list when no new traces have been written.
- All extensions work via structural subtyping (duck typing). There is no base class to inherit from and no registration step -- just implement the protocol methods.
