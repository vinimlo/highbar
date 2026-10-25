# Installation

## Requirements

- **Python 3.12+**

## Full Install

Install Highbar with all dependencies (CLI, SDK, and runtime):

```bash
pip install highbar
```

This includes everything you need: the `highbar` CLI, the `@hb.tool` decorator SDK, and all dependencies (`pyyaml`, `rich`, `typer`, `httpx`).

## SDK-Only Install

For production agent code where you only need the `@hb.tool` decorator:

```bash
pip install highbar[sdk]
```

This is a slim install with **zero external dependencies**. The decorator is a complete no-op when the `HIGHBAR_TRACES_URL` environment variable is not set, so there is no runtime overhead in production.

## Using uv

If you use [uv](https://docs.astral.sh/uv/) as your package manager:

```bash
# Full install
uv add highbar

# SDK-only install
uv add highbar[sdk]
```

## Verify Installation

After installing, verify the CLI is available:

```bash
highbar --help
```

Expected output:

```
 Usage: highbar [OPTIONS] COMMAND [ARGS]...

 Behavior testing framework for AI agents.

Options:
  --help  Show this message and exit.

Commands:
  init      Scaffold a highbar.config.yaml and example spec.
  run       Run behavior tests against your agent.
  validate  Validate spec syntax without running tests.
```

## Scaffold an Example

To generate an example spec file and get started quickly:

```bash
highbar init
```

This creates a `specs/` directory with an `example.highbar.yaml` file you can edit and run.

## Next Steps

- [Quickstart](/getting-started/quickstart) — build and test your first agent in 5 minutes
- [Core Concepts](/getting-started/concepts) — understand specs, traces, and the execution model
