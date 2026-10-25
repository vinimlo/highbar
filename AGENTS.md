# AGENTS.md — Highbar

## Overview

Highbar is an open-source behavior testing framework for AI agents — "Playwright for AI Agents". It tests the **agent** (not the LLM) by defining expected behaviors in YAML specs, instrumenting tool calls via a Python decorator (`@hb.tool`), and running assertions against collected traces.

## Tech Stack

- **Language**: Python 3.12+ (strict typing)
- **Package manager**: uv
- **Testing**: pytest
- **Linting/Formatting**: ruff
- **CLI framework**: typer + rich

## Commands

```bash
make test                     # run unit + integration tests
make lint                     # lint
make format                   # format
uv run highbar --help        # CLI help
uv run highbar run specs/    # run all specs
uv run highbar validate <spec> # validate spec syntax
uv run highbar init          # scaffold example spec

# Documentation (local via npm)
make docs-install             # install site dependencies
make docs-dev                 # start dev server
make docs-build               # production build
make docs-preview             # preview production build

# Documentation (Docker)
make docs-up                  # start docs container
make docs-down                # stop docs container
```

## Architecture

```
YAML Spec → Parser → Runner → Assertions → Reporter
                       ↓              ↑
              SubprocessAdapter    FileTraceCollector
              (starts agent)       (reads JSONL traces)
                       ↓              ↑
                  Agent (with @hb.tool decorator → TraceWriter)
```

1. **SDK** (`src/highbar/sdk/`) — `Highbar` class with `@hb.tool` decorator. Instruments tool functions, writes atomic trace boxes (fingerprinted JSONL) when `HIGHBAR_TRACES_URL` env var is set. Zero overhead when disabled.
2. **Parser** (`src/highbar/engine/parser.py`) — reads `.highbar.yaml` files, validates schema, produces domain models
3. **Templates** (`src/highbar/engine/templates.py`) — resolves `{{previous.*}}` expressions from trace history
4. **Runner** (`src/highbar/engine/runner.py`) — orchestrates test execution: starts agent subprocess, sends HTTP triggers, collects traces after settle time, runs assertions
5. **Assertions** (`src/highbar/assertions/`) — function_calls (name + subset params + result), response (contains, matches, not_contains), result (subset match on return values)
6. **Adapters** (`src/highbar/adapters/`) — SubprocessAdapter (agent lifecycle), FileTraceCollector (JSONL reader), HTTPClient (trigger sender)
7. **Reporter** (`src/highbar/reporters/terminal.py`) — rich-formatted terminal output

## Key Conventions

- Spec files use `.highbar.yaml` extension
- Default spec directory: `specs/`
- Domain models are frozen dataclasses in `src/highbar/domain/models.py`
- Extension points use `Protocol` classes in `src/highbar/domain/protocols.py`
- Assertions implement the `Assertion` protocol (evaluate → list[AssertionResult])
- Multi-turn state is threaded via `{{previous.*}}` template syntax
- Tests that assert `function_calls: []` are guardrail/negative tests
- Trace boxes use sha256 fingerprint + sequential numbering for correlation
- Agent is started as subprocess with `HIGHBAR_TRACES_URL` injected

## Project Structure

```
src/highbar/
├── __init__.py           # re-exports Highbar class
├── sdk/
│   ├── core.py           # Highbar class, tool registry
│   ├── decorator.py      # @hb.tool wrapper logic
│   └── trace.py          # TraceWriter: fingerprint, seq, JSONL append
├── domain/
│   ├── models.py         # dataclasses: Spec, Suite, Test, Trace, etc.
│   └── protocols.py      # Protocol interfaces: Assertion, Reporter, etc.
├── engine/
│   ├── parser.py         # YAML spec parser + validator
│   ├── templates.py      # {{previous.*}} template resolver
│   └── runner.py         # Test execution orchestrator
├── assertions/
│   ├── function_calls.py # tool invocation assertions
│   ├── response.py       # text/regex response assertions
│   └── result.py         # tool return value assertions
├── adapters/
│   ├── subprocess_adapter.py  # agent lifecycle management
│   ├── file_collector.py      # JSONL trace reader
│   └── http_client.py         # HTTP trigger sender
├── reporters/
│   └── terminal.py       # rich terminal reporter
└── cli/
    └── main.py           # typer CLI (run, validate, init)
tests/
├── unit/                 # 78 unit tests
└── integration/          # end-to-end tests with real FastAPI agent
specs/examples/           # example .highbar.yaml files
docs/                     # design docs and future plans
pitch/                    # original pitch deck
```

## Package Distribution

- `pip install highbar` — installs everything (CLI + SDK + all dependencies)
- `pip install highbar[sdk]` — slim install for production (decorator only, zero extra deps)
