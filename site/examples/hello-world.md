# Hello World Example

This walkthrough covers the simplest possible Gauntlet spec -- a single test that verifies an agent calls a tool with the correct arguments.

## The Spec

Create a file at `specs/booking.gauntlet.yaml`:

```yaml
agent:
  start: "python -m my_agent.server"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Booking
    tests:
      - trigger: "I need to book a flight to Tokyo"
        expect:
          function_calls:
            - name: search_flights
              params:
                destination: Tokyo
```

## Line by Line

### Agent Block

```yaml
agent:
  start: "python -m my_agent.server"
```

The `start` field is the shell command Gauntlet executes to launch your agent. This command runs as a subprocess with the `GAUNTLET_TRACES_URL` environment variable injected.

```yaml
  endpoint: "http://localhost:8000/chat"
```

The `endpoint` is where Gauntlet sends POST requests with the trigger message. The request body is `{"message": "I need to book a flight to Tokyo"}`.

```yaml
  health: "http://localhost:8000/health"
```

The `health` URL is polled every 200ms until it returns HTTP 200. This ensures the agent is fully started before tests begin. If omitted, Gauntlet sends the first trigger immediately.

### Suites and Tests

```yaml
suites:
  - name: Booking
```

A suite is a named group of tests. The name appears in the test output.

```yaml
    tests:
      - trigger: "I need to book a flight to Tokyo"
```

The `trigger` is the user message sent to the agent. This simulates a real user interaction.

```yaml
        expect:
          function_calls:
            - name: search_flights
              params:
                destination: Tokyo
```

The `expect` block defines what Gauntlet checks after the agent responds:

- `name: search_flights` -- the agent must call a tool named `search_flights`.
- `params: {destination: Tokyo}` -- the call must include `destination: "Tokyo"` in its arguments. Other arguments are allowed (subset matching).

## The Agent

For this spec to work, your agent needs the `@gt.tool` decorator on its `search_flights` function:

```python
from gauntlet import Gauntlet

gt = Gauntlet()

@gt.tool
def search_flights(destination: str, date: str | None = None) -> dict:
    # Your actual flight search logic
    return {
        "flights": [
            {"airline": "JAL", "price": 850, "departure": "2024-03-15"},
            {"airline": "ANA", "price": 920, "departure": "2024-03-15"},
        ]
    }
```

The `@gt.tool` decorator captures the function name, arguments, return value, and timing whenever the function is called. In production (without `GAUNTLET_TRACES_URL`), the decorator has zero overhead.

## Running the Test

```bash
gauntlet run specs/booking.gauntlet.yaml
```

### Expected Output (Pass)

```
  Gauntlet v0.1.0
  Running specs/booking.gauntlet.yaml

  Booking
    ✓ I need to book a flight to Tokyo                    145ms

  1 passed, 0 failed (2.3s)
```

### Expected Output (Fail)

If the agent calls a different function or passes wrong arguments:

```
  Gauntlet v0.1.0
  Running specs/booking.gauntlet.yaml

  Booking
    ✗ I need to book a flight to Tokyo                    130ms
      ✗ Expected function 'search_flights' not found in traces

  0 passed, 1 failed (2.1s)
```

## What Happens Under the Hood

1. Gauntlet parses the YAML spec.
2. Creates a temporary trace file at `/tmp/gauntlet-traces-xyz.jsonl`.
3. Starts `python -m my_agent.server` with `GAUNTLET_TRACES_URL=/tmp/gauntlet-traces-xyz.jsonl`.
4. Polls `http://localhost:8000/health` until it returns 200.
5. Records a checkpoint (line count = 0 in the empty trace file).
6. Sends `POST http://localhost:8000/chat` with `{"message": "I need to book a flight to Tokyo"}`.
7. The agent processes the message, calls `search_flights(destination="Tokyo")`.
8. The `@gt.tool` decorator writes a trace box to the JSONL file.
9. The agent returns an HTTP response.
10. Gauntlet waits 500ms (settle time).
11. Reads the trace file -- finds one trace for `search_flights` with `args.destination = "Tokyo"`.
12. Assertion passes: function name matches, params are a subset of actual args.
13. Reports the result and stops the agent.

## Next Steps

- Add response assertions: [Spec Format Reference](/reference/spec-format)
- Test multi-turn conversations: [Order Support Example](/examples/order-support)
- Add guardrail tests: [Guardrail Suite](/examples/guardrail-suite)
