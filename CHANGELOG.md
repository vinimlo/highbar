# Changelog

## 0.1.0rc2

### Fixed

- A guardrail on a tool highbar cannot see no longer passes. An onboarding rehearsal of 0.1.0rc1 found two ways it did: `forbid` on a tool without `@hb.tool`, and `forbid` on a name typed wrong. The agent now declares the tools it records, and a name in `forbid` or `function_calls` outside them makes the test an `error` that lists what the agent records and the closest name. An agent that declares nothing gets a warning.

### Added

- `hb.declare(*names)`, for tools recorded with `hb.record()`. `@hb.tool` declares its function itself.
- Trace contract v1: an optional `tools` record, and an optional `tools` field in `hello`. Older versions of highbar ignore both.

### Changed

- The connecting guide checks a connection with one `function_calls` test per tool, not with a guardrail.
- Every GitHub Action in CI, Pages and the release runs on Node 24.

## 0.1.0rc1

The first release candidate of highbar: evals for AI agents that behave like tests.

### Specs

- A YAML spec states what the agent must do for each message. Unknown keys, wrong types, invalid regular expressions and tests that check nothing are errors before anything runs, with a "did you mean".
- `function_calls`: each expectation needs a call of its own, matched on `name`, `params` and `result`. A call that failed counts only where the spec expects the failure (`error`).
- `ordered` (by when calls ran), `exclusive` (nothing extra) and `forbid` (tools that must not be called).
- `response`: `contains`, `icontains`, `not_contains`, `not_icontains`, `matches`, `not_matches` and `equals`, also usable inside `params` and `result`.
- Multi-turn tests, with values from earlier turns in later triggers (`{{previous.*}}`).
- `runs` and `min_pass`: repeat a test and state how many runs must pass; `--runs` for the whole suite.
- `variations`: paraphrases of a trigger, generated once by an OpenAI-compatible model and committed.

### Running the agent

- Managed mode: highbar starts the agent in its own process group, waits for it, logs its output to `.highbar/logs/`, and stops it, even when interrupted. A busy port or an agent that dies at start stops the run with the reason.
- Attach mode: tests run against an agent that is already up, with a configurable request (`method`, `headers`, `body`), a reply path, a timeout, and `${VAR}` values: one of 6 characters or more never appears in the output.
- Session hooks (commands or Python functions) open a clean session per test execution.

### Results

- Three states: `passed`, `failed`, and `error` when the behavior could not be verified. A test that checks tool calls needs proof that the agent recorded its message.
- Each test execution has an id, sent in `X-Highbar-Test`; trace records are matched to their test by it, or by `agent.correlate`. A late call fails the run it belongs to.
- Exit codes: 0 all passed, 1 a test failed or errored, 2 nothing could run; 130, 143 and 129 when interrupted (Ctrl-C, SIGTERM, SIGHUP), after the agent is stopped.

### Connecting an agent

- The trace contract v1: one JSON line per tool call, message handled, and writer start. Version 0 lines are read too.
- Python SDK, standard library only: `@hb.tool` (sync and async), `hb.record()` for dispatch funnels, `hb.turn()`, and `highbar.asgi.Middleware`.
- `highbar.run()` runs a spec from Python.

### Getting started

- `highbar init` writes a quickstart agent and its spec; `pip install "highbar[quickstart]"`, `highbar init` and `highbar run` give a first green.
- Documentation at https://vinimlo.github.io/highbar/, where every spec shown is validated by the test suite.
