# AGENTS.md — highbar

## What it is

highbar runs evals for AI agents that behave like tests. A YAML spec says what the agent must do for a message: which tools it calls, in which order, with what, and what it answers. highbar sends the message to the running agent, reads the trace records the agent wrote while handling it, and checks them. A green result means the behavior was verified; anything highbar could not verify is an `error`, never a pass.

## Stack

Python ≥ 3.12, uv, typer + rich (CLI), httpx, pyyaml, pytest, ruff, pyright (strict on `src`). The SDK (`import highbar`, `highbar.sdk`, `highbar.asgi`) loads only the standard library, so it can live in an agent's production image.

## Commands

```bash
make test          # unit + integration tests
make lint          # ruff check
make format        # ruff format
make typecheck     # pyright strict on src/
uv run highbar --help
uv run highbar run [spec-or-dir]       # default: every *.highbar.yaml under specs/
uv run highbar validate [spec-or-dir]
make docs-up       # the documentation site in Docker, on http://localhost:4001
make docs-down
```

Before a commit: `make lint`, `uv run ruff format --check src/ tests/`, `make typecheck` and `make test`.

## How a run works

1. **Parse.** `engine/parser.py` reads the spec against `engine/schema.py`: an unknown key, a wrong type, an invalid regex or a test that asserts nothing is a spec error (exit 2) with a "did you mean".
2. **The agent.** Managed mode (`agent.start`): highbar starts the agent in its own process group, sets `HIGHBAR_TRACES_URL` to a fresh trace file, logs its output to `.highbar/logs/`, and stops it at the end. Attach mode (no `start`): the agent is already running and writes `agent.traces` itself; highbar never stops it.
3. **Each test execution** gets an id, sent in the `X-Highbar-Test` header and available as `{{test.id}}`. Session hooks (`agent.session`) open a clean session per execution. The request and the reply path are configurable (`agent.request`, `agent.response`).
4. **Observation.** The collector reads the trace file from a byte offset: `tool`, `turn` and `hello` records (contract v1; v0 lines are read too). `engine/observation.py` keeps the records whose `context` matches `agent.correlate` (default `{test: "{{test.id}}"}`). A test that checks tool calls needs a record of its own; without one it is `error` ("not observed").
5. **Assertions.** `function_calls` pairs each expectation with a distinct call (bipartite matching), with `error`, `ordered`, `exclusive` and `forbid`; `response` uses the matchers of `params` (`contains`, `matches`, `not_matches`...). A call's `error` field is the only signal that a tool failed.
6. **Repetition.** Each phrasing runs `runs` times and must reach `min_pass`. A late call (recorded after its window) fails its own run, and the test is judged again.
7. **Results.** Three states: `passed`, `failed` (the agent did something else), `error` (could not verify). Exit codes: 0 all passed, 1 something failed or errored, 2 could not run at all.

## Modules

```
src/highbar/
├── __init__.py            # public API: Highbar (SDK), asgi, run
├── api.py                 # highbar.run(): a spec from Python
├── asgi.py                # middleware: a turn record per HTTP request
├── _narrow.py             # as_dict / as_list for values read from YAML or JSON
├── sdk/                   # core.py (Highbar: tool, record, turn), decorator.py, trace.py, context.py
├── domain/                # models.py (frozen dataclasses), protocols.py (extension points)
├── engine/
│   ├── parser.py, schema.py      # spec → domain models, strict
│   ├── run_spec.py               # one spec against its agent (CLI and api)
│   ├── runner.py                 # executions, windows, late calls, runs
│   ├── observation.py            # which records belong to a test
│   ├── runs.py                   # runs and min_pass
│   ├── placeholders.py, paths.py, templates.py
│   ├── session.py                # session hooks (commands or Python)
│   └── secrets.py                # ${VAR} values masked in every message
├── adapters/              # subprocess_adapter.py, attached.py, http_client.py, file_collector.py, ports.py
├── assertions/            # function_calls.py, response.py, _match.py
├── variations/            # LLM paraphrases of a trigger, cached in a committed JSON file
├── reporters/terminal.py
└── cli/main.py            # run, validate, init, variations
tests/
├── unit/                  # one directory per package
├── integration/           # the real CLI against tests/fixtures/server.py
└── fixtures/server.py     # a stand-in agent with switches for each failure mode
```

## Conventions

- A green that does not mean "verified" is the worst bug this project can have. Every path to a false green gets a regression test named after its finding code (`test_a7_...`, `test_c1_...`): the codes come from the reviews that found them.
- Messages say what happened and what to do next, in one line. They never show a value that came from `${VAR}`.
- Domain models are frozen dataclasses; extension points are `Protocol`s in `domain/protocols.py`.
- Nothing in the SDK may raise into the agent: a trace write that fails is logged and swallowed.
- Code, messages, docs and commits are in English. Commits follow Conventional Commits.

## Workflow

- Branch from `develop` (`feat/`, `fix/`, `docs/`, `chore/`), open the PR against `develop`. `main` receives releases.
- A PR says what changed and shows it: the output before and after, copied from a real run.
- Agent instructions live in this file only; there is no `CLAUDE.md`.
