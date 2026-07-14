# Domain Models

All domain models are frozen dataclasses defined in `gauntlet.domain.models`. Frozen dataclasses are immutable after creation, which prevents accidental mutation during test execution.

---

## Trace

An atomic box capturing a single tool invocation.

```python
@dataclass(frozen=True)
class Trace:
    id: str
    seq: int
    fn: str
    args: dict[str, Any]
    result: Any
    started_at: float
    ended_at: float
    error: str | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | - | Unique fingerprint ID (16-char hex from sha256) |
| `seq` | `int` | - | Auto-incrementing sequence number |
| `fn` | `str` | - | Function name |
| `args` | `dict[str, Any]` | - | Function arguments |
| `result` | `Any` | - | Return value |
| `started_at` | `float` | - | Invocation start timestamp (`time.time()`) |
| `ended_at` | `float` | - | Invocation end timestamp |
| `error` | `str \| None` | `None` | Error message if the function raised an exception |

**Computed property:**

| Property | Type | Description |
|----------|------|-------------|
| `duration_ms` | `float` | `(ended_at - started_at) * 1000` |

---

## AgentConfig

Configuration for the agent under test, parsed from the `agent:` block in a spec file.

```python
@dataclass(frozen=True)
class AgentConfig:
    start: str
    endpoint: str
    health: str | None = None
    startup_timeout: int = 15000
    settle_time: int = 500
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start` | `str` | - | Shell command to start the agent |
| `endpoint` | `str` | - | HTTP URL for POST triggers |
| `health` | `str \| None` | `None` | Health check URL (polled until HTTP 200) |
| `startup_timeout` | `int` | `15000` | Max milliseconds to wait for health check |
| `settle_time` | `int` | `500` | Milliseconds to wait after response before reading traces |

---

## FunctionCallExpect

Expected function call assertion, parsed from entries in `expect.function_calls`.

```python
@dataclass(frozen=True)
class FunctionCallExpect:
    name: str
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | - | Expected function name |
| `params` | `dict[str, Any] \| None` | `None` | Expected arguments (subset match) |
| `result` | `dict[str, Any] \| None` | `None` | Expected return value (subset match) |

---

## ResponseExpect

Expected response text assertion, parsed from `expect.response`.

```python
@dataclass(frozen=True)
class ResponseExpect:
    contains: str | None = None
    not_contains: str | None = None
    matches: str | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `contains` | `str \| None` | `None` | Substring that must be present |
| `not_contains` | `str \| None` | `None` | Substring that must not be present |
| `matches` | `str \| None` | `None` | Regex pattern (Python `re.search`) |

---

## Expect

Expected outcomes for a test or turn, combining function call and response assertions.

```python
@dataclass(frozen=True)
class Expect:
    function_calls: list[FunctionCallExpect] | None = None
    response: ResponseExpect | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `function_calls` | `list[FunctionCallExpect] \| None` | `None` | Expected tool invocations; `[]` means no tools allowed |
| `response` | `ResponseExpect \| None` | `None` | Text assertions on the response |

---

## Turn

A single turn in a multi-turn test.

```python
@dataclass(frozen=True)
class Turn:
    trigger: str
    expect: Expect
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trigger` | `str` | - | The message sent to the agent |
| `expect` | `Expect` | - | Expected outcomes for this turn |

---

## Test

A single test case. Either `trigger` + `expect` (single-turn) or `turns` (multi-turn). Never both.

```python
@dataclass(frozen=True)
class Test:
    trigger: str | None = None
    expect: Expect | None = None
    turns: list[Turn] | None = None
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trigger` | `str \| None` | `None` | Message sent to the agent (single-turn) |
| `expect` | `Expect \| None` | `None` | Expected outcomes (single-turn) |
| `turns` | `list[Turn] \| None` | `None` | Ordered list of turns (multi-turn) |

---

## Suite

A named group of tests.

```python
@dataclass(frozen=True)
class Suite:
    name: str
    tests: list[Test]
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | - | Suite name, displayed in test output |
| `tests` | `list[Test]` | - | List of test cases |

---

## Spec

A complete spec file containing agent configuration and test suites.

```python
@dataclass(frozen=True)
class Spec:
    agent: AgentConfig
    suites: list[Suite]
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent` | `AgentConfig` | - | Agent configuration |
| `suites` | `list[Suite]` | - | List of test suites |

---

## AssertionResult

Result of a single assertion evaluation.

```python
@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    message: str
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `passed` | `bool` | - | Whether the assertion passed |
| `message` | `str` | - | Human-readable description of the outcome |

---

## TestResult

Result of running a single test.

```python
@dataclass(frozen=True)
class TestResult:
    passed: bool
    errors: list[str]
    duration_ms: float
    traces: list[Trace] = field(default_factory=list)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `passed` | `bool` | - | Whether all assertions passed |
| `errors` | `list[str]` | - | List of failure messages |
| `duration_ms` | `float` | - | Total test duration in milliseconds |
| `traces` | `list[Trace]` | `[]` | Trace boxes collected during the test |

---

## RunSummary

Summary of a complete test run across all suites.

```python
@dataclass(frozen=True)
class RunSummary:
    total: int
    passed: int
    failed: int
    duration_ms: float
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `total` | `int` | - | Total number of tests executed |
| `passed` | `int` | - | Number of passing tests |
| `failed` | `int` | - | Number of failing tests |
| `duration_ms` | `float` | - | Total run duration in milliseconds |
