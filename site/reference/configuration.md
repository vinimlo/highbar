# Configuration Reference

Highbar is configured through spec files and environment variables. There is no global configuration file -- all settings live in the spec YAML or are injected automatically.

## Environment Variables

### `HIGHBAR_TRACES_URL`

The path to the JSONL trace file used by the SDK to write trace boxes.

| Detail | Value |
|--------|-------|
| **Set by** | Highbar CLI (automatically injected into the agent subprocess) |
| **Read by** | `Highbar()` SDK constructor |
| **Format** | Absolute file path (e.g., `/tmp/highbar-traces-abc123.jsonl`) |

When `HIGHBAR_TRACES_URL` is not set, the `@hb.tool` decorator passes through with zero overhead. This means the same agent code runs in production and under test without any conditional logic.

You can also pass the trace path explicitly:

```python
hb = Highbar(traces="/tmp/my-traces.jsonl")
```

## Default Spec Directory

When running `highbar run` without arguments, Highbar discovers spec files by recursively searching the `specs/` directory (relative to the current working directory) for files matching `*.highbar.yaml`.

```
project/
  specs/
    order-support.highbar.yaml    # discovered
    auth/
      login-flow.highbar.yaml     # discovered (recursive)
  src/
    agent.py
```

## Default Timeouts

| Setting | Default | Unit | Description |
|---------|---------|------|-------------|
| `startup_timeout` | 15000 | ms | Maximum time to wait for the agent's health check to return HTTP 200 |
| `settle_time` | 500 | ms | Time to wait after receiving a response before reading traces |
| HTTP request timeout | 30000 | ms | Timeout for each POST trigger sent to the agent endpoint |
| Health poll interval | 200 | ms | Interval between health check polling requests |

All timeout values can be overridden in the spec's `agent` block except the HTTP request timeout and health poll interval, which are fixed in the current version.

### Startup Timeout

```yaml
agent:
  start: "python -m my_agent.server"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"
  startup_timeout: 30000  # 30 seconds for slow-starting agents
```

### Settle Time

```yaml
agent:
  start: "python -m my_agent.server"
  endpoint: "http://localhost:8000/chat"
  settle_time: 2000  # 2 seconds for agents with async tool calls
```

## Agent Shutdown

When tests complete (or an error occurs), Highbar shuts down the agent subprocess using a two-phase approach:

1. **Terminate** (`SIGTERM`): sends a graceful termination signal.
2. **Wait** (5 seconds): waits for the process to exit.
3. **Kill** (`SIGKILL`): if the process has not exited after 5 seconds, forces termination.

This ensures that agents with cleanup logic (closing database connections, flushing logs) have time to shut down gracefully, while preventing zombie processes.

## HTTP Trigger Format

Highbar sends triggers to the agent endpoint as POST requests with a JSON body:

```json
{"message": "user trigger text here"}
```

The agent's response is parsed from JSON. Highbar checks for these fields in order: `response`, `message`, `content`, `text`, `reply`. The first match is used as the response text for assertions.

```json
// Any of these response formats work:
{"response": "Here is your order status..."}
{"message": "Here is your order status..."}
{"content": "Here is your order status..."}
```

If none of these keys are found, the entire response body is converted to a string.
