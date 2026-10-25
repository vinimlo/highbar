# Agent Setup

The `agent` block in your spec file tells Highbar how to start, communicate with, and manage the lifecycle of your agent process.

## Configuration Fields

### `start` (required)

The shell command to start your agent. Highbar runs this as a subprocess using `subprocess.Popen` with `shlex.split` for argument parsing.

```yaml
agent:
  start: "python -m my_agent.server"
```

```yaml
agent:
  start: "uvicorn my_agent:app --host 0.0.0.0 --port 8000"
```

The command runs with the current working directory inherited from where you invoked `highbar run`.

### `endpoint` (required)

The HTTP URL where Highbar sends POST requests with the trigger message. Each trigger is sent as a JSON body:

```json
{"message": "trigger text here"}
```

```yaml
agent:
  endpoint: "http://localhost:8000/chat"
```

### `health` (optional)

A URL that Highbar polls with GET requests until it receives an HTTP 200 response. Polling occurs at 200ms intervals. If omitted, Highbar starts sending test triggers immediately after launching the subprocess.

```yaml
agent:
  health: "http://localhost:8000/health"
```

Using a health check is recommended for agents that need initialization time (loading models, connecting to databases, etc.).

### `startup_timeout` (optional, default: 15000)

Maximum time in milliseconds to wait for the health check to succeed. If the health endpoint does not return HTTP 200 within this window, Highbar terminates the agent subprocess and raises a `TimeoutError`.

```yaml
agent:
  startup_timeout: 30000  # 30 seconds for slow-starting agents
```

### `settle_time` (optional, default: 500)

Time in milliseconds to wait after receiving the agent's HTTP response before reading trace files. This accounts for any asynchronous tool calls that complete after the response is sent.

```yaml
agent:
  settle_time: 1000  # 1 second for agents with async tool execution
```

## Agent Lifecycle

When you run `highbar run`, the following sequence occurs for each spec file:

```
1. Create temporary JSONL file for traces
2. Start agent subprocess
   └─ Inject HIGHBAR_TRACES_URL env var pointing to the trace file
3. Poll health endpoint (if configured)
   └─ GET requests every 200ms until HTTP 200 or timeout
4. Run all suites and tests
   └─ For each test trigger:
       a. POST {"message": "..."} to the endpoint
       b. Wait settle_time milliseconds
       c. Read new traces from the JSONL file
       d. Run assertions against traces and response
5. Terminate agent subprocess
   └─ Send SIGTERM, wait 5 seconds
   └─ If still running, send SIGKILL
```

## Response Format

Highbar expects your agent's endpoint to return JSON. It extracts the response text by checking for these keys in order:

1. `response`
2. `message`
3. `content`
4. `text`
5. `reply`

The first key found is used as the response text. If none of these keys exist, the entire JSON response is converted to a string.

**Examples of valid response formats:**

```json
{"response": "I found 3 orders for your account."}
```

```json
{"message": "Your order #123 has been shipped."}
```

```json
{"content": "Here are the results...", "metadata": {"tokens": 150}}
```

## Environment Variables

Highbar injects the following environment variable into your agent's subprocess:

| Variable | Value | Purpose |
|----------|-------|---------|
| `HIGHBAR_TRACES_URL` | Path to a temporary `.jsonl` file | Tells the SDK where to write trace records |

Your agent inherits all environment variables from the parent process, plus `HIGHBAR_TRACES_URL`. This means API keys, database URLs, and other configuration set in your shell are available to the agent.

## Example: Minimal Agent

A minimal FastAPI agent compatible with Highbar:

```python
from fastapi import FastAPI
from pydantic import BaseModel
from highbar import Highbar

app = FastAPI()
hb = Highbar()

class ChatRequest(BaseModel):
    message: str

@hb.tool
def get_weather(city: str) -> dict:
    return {"city": city, "temp": 22, "condition": "sunny"}

@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    # Your LLM routing logic here
    result = get_weather(city="London")
    return {"response": f"The weather in London is {result['condition']}."}

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
```

Matching spec:

```yaml
agent:
  start: "uvicorn my_agent:app --port 8000"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Weather
    tests:
      - trigger: "What's the weather in London?"
        expect:
          function_calls:
            - name: get_weather
              params:
                city: "London"
```

## Tips

- **Always use a health check** for agents that take more than a few milliseconds to start. Without it, Highbar may send triggers before the agent is ready.
- **Increase `settle_time`** if your agent performs tool calls asynchronously after returning the HTTP response.
- **Increase `startup_timeout`** for agents that load large models or connect to external services during initialization.
- **Use a dedicated port** to avoid conflicts with other services running on your development machine.
