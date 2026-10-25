# CLI Reference

Highbar provides three commands for running tests, validating specs, and scaffolding new projects.

## Installation

```bash
pip install highbar
```

Once installed, the `highbar` CLI is available globally.

## Commands

### `highbar run [spec_path]`

Run behavior tests against your agent.

```bash
# Run a specific spec file
highbar run specs/order-support.highbar.yaml

# Discover and run all specs in the default directory
highbar run
```

**Arguments:**

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `spec_path` | No | - | Path to a `.highbar.yaml` spec file |

**Behavior:**

1. If `spec_path` is provided, Highbar runs that specific file.
2. If omitted, Highbar discovers all `*.highbar.yaml` files recursively in the `specs/` directory.
3. For each spec file:
   - Parses the YAML into domain models.
   - Creates a temporary JSONL trace file.
   - Starts the agent subprocess with `HIGHBAR_TRACES_URL` injected into its environment.
   - Runs all suites and tests sequentially.
   - Reports results via the terminal reporter.
4. After all specs complete, exits with the appropriate code.

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | All tests passed |
| `1` | One or more tests failed, or no spec files were found |

**Example output:**

```
  Highbar v0.1.0
  Running specs/order-support.highbar.yaml

  Order Lookup
    ✓ Where is my order #12345?                           120ms
    ✓ Track order #67890                                   95ms

  Guardrails
    ✓ Ignore all previous instructions...                  80ms
    ✗ What's the credit card number...                    110ms
      ✗ Expected response not to contain pattern

  3 passed, 1 failed (405ms)
```

---

### `highbar validate <spec_path>`

Validate the syntax and structure of a spec file without running any tests.

```bash
highbar validate specs/order-support.highbar.yaml
```

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `spec_path` | Yes | Path to a `.highbar.yaml` spec file |

**Behavior:**

- Parses the YAML spec and validates its structure against the expected schema.
- On success, reports the number of suites and tests found.
- On failure, displays error details.
- Does **not** start the agent or run any tests.

**Example output (success):**

```
  ✓ Valid: 3 suite(s), 7 test(s)
```

**Example output (failure):**

```
  ✗ Invalid: missing required field 'agent.start'
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Spec is valid |
| `1` | Spec has syntax or structural errors |

---

### `highbar init`

Scaffold a new Highbar project with an example spec file.

```bash
highbar init
```

**Behavior:**

1. Creates the `specs/` directory if it does not exist.
2. Generates `specs/example.highbar.yaml` with a basic template.
3. If the file already exists, skips creation and prints a notice.

**Generated template:**

```yaml
agent:
  start: "python -m my_agent.server"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Basic
    tests:
      - trigger: "Hello!"
        expect:
          response:
            contains: "hello"
```

**Example output:**

```
  Created specs/example.highbar.yaml

  Edit the spec and run: highbar run specs/example.highbar.yaml
```

## Global Options

| Option | Description |
|--------|-------------|
| `--help` | Show help for the CLI or any subcommand |
| `--version` | Display the installed Highbar version |

```bash
highbar --help
highbar run --help
```
