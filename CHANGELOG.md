# Changelog

## Unreleased

### Added

- Tokens and cost. A `turn` record can carry an optional `usage`: `model`, `provider`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost`, `currency` and `llm_calls`, all optional. The contract stays at v1, and a writer without it works as before. In Python, set `turn.usage` in `hb.turn()`.
- A price table for the cost a writer does not send: `src/highbar/engine/prices.json`, in USD per token, in the format of OpenRouter's model list, rewritten by `scripts/update_prices.py`. A cost the writer sends always stands. A model outside the table leaves the cost missing, with a warning.
- Each turn of the report has its `usage`, and each execution, test, suite and spec, and the run, have `totals`. A sum holds only what is known: when a cost or a currency is missing, or the costs come in more than one currency, `complete` is `false` and `warnings` says what is missing. Costs in different currencies are never added together. The terminal's summary ends with the tokens and cost.
- `expect.budget`, with `max_input_tokens` and `max_cost`: per turn in a turn's `expect`, and for the whole execution in a test's. A multi-turn test can now have an `expect` that holds only a `budget`. `defaults.budget` applies to the tests without their own. Over a limit, the run fails and counts against `min_pass`; a budget without the usage to check it is an `error`. Usage follows the rules of tool calls: a `turn` with no test context counts for no test, and a budget that holds only without it is an `error`; a `turn` recorded late counts for its test, which is judged again.
- `highbar run --max-cost AMOUNT`: a ceiling on what the whole run spends. Over it, the run stops at the end of the test in progress, keeps its results, reports `stopped.reason: "budget_exceeded"` and exits with the new code 3.
- `imatches` and `not_imatches`: `matches` and `not_matches`, ignoring case, in `response`, `params` and `result`.
- `defaults.min_pass`: the threshold for every test without its own `min_pass`, stated once instead of in each test. It takes the same values as the test's: a whole number of runs, or a fraction of each test's runs, rounded up and computed again with `--runs`. A test's own `min_pass` still wins; without either, every run must pass, as before. A whole number above a test's runs is an error in `validate` and before `run` starts the agent, naming the test and `defaults.min_pass`.
- `highbar run --report json=<file>`: the results as JSON, with one test, phrasing, execution and turn per entry, in their final state. The file is replaced before the first message and written on every way out of the run, with the code the process returns: a run that dies leaves `exit_code: null`, never an older green. See the report reference.
- An optional `id` on a test, and a stable key for each one: its `id`, or `<suite>/<name>`.
- `definition_sha256` on each test of the report, to tell whether two runs measured the same thing.
- `highbar.run()` returns each test in `summary.tests` and the warnings in `summary.warnings`, and takes a `reporter`.

### Fixed

- A value from `${VAR}` is masked in every spec of a run, not only in the spec that uses it, and when a message is cut in the middle of it. Two or fewer of its first characters can still show at a cut: telling a shorter start from an ordinary word is not possible.

### Upgrading from 0.1.0

- A spec with two tests of the same key no longer parses. The key is `<suite>/<name>`, and a test without a name uses its trigger, or its number of turns for a multi-turn test. So this reaches two such tests in one suite, in two suites with the same name, and in two suites without a name. Give one an `id` or a different `name`. The key is what tells them apart in a report, and numbering them would tie it to their position.
## 0.1.0

The first final release. The code is the code of 0.1.0rc3, which has been running a production agent's evals since 2026-10-27: only the version changed.

## 0.1.0rc3

### Fixed

- Attach mode no longer sends a single message when the trace file does not exist. When a test checks tool calls, the run stops before the first test with exit code 2 and the file's absolute path. In the rc1 onboarding rehearsal, a wrong `traces` path sent every message, real transfers included, and only then said nothing was observed.
- The warning about tool calls without a test context no longer contradicts the results. Next to tagged records, an untagged call counts for no test, and the warning now says so. Against an attached agent, a test that checks tool calls and has no tagged record is an `error`, without the warning. The warnings and that error name the keys of `agent.correlate` (`hb.turn(conversation_id=...)`) instead of always the test id.

### Upgrading from 0.1.0rc2

- An attach-mode spec that checks tool calls now needs the trace file to exist before the run. highbar's SDK creates it when `Highbar()` is created, and after the file is deleted, only with its next record. Create the file empty when it was deleted while the agent ran, or when its writer creates it only with its first record (such as a v0 emitter on a new file).

## 0.1.0rc2

### Fixed

- A guardrail on a tool highbar cannot see no longer passes. An onboarding rehearsal of 0.1.0rc1 found two ways it did: `forbid` on a tool without `@hb.tool`, and `forbid` on a name typed wrong. The agent now declares the tools it records, and a name in `forbid` or `function_calls` outside them makes the test an `error` that lists what the agent records and the closest name. An agent that declares nothing gets a warning.

### Added

- `hb.declare(*names)`, for tools recorded with `hb.record()`. `@hb.tool` declares its function itself.
- Trace contract v1: an optional `tools` record, and an optional `tools` field in `hello`. Older versions of highbar ignore both.

### Changed

- The connecting guide checks a connection with one `function_calls` test per tool, not with a guardrail.
- When the agent has declared nothing, the warning about guardrails comes once, at the end of the run, and names every one.
- The SDK writes its `hello` and its tools again when the trace file is emptied or replaced.
- Every GitHub Action in CI, Pages and the release runs on Node 24.

### Upgrading from 0.1.0rc1

A spec that was green on 0.1.0rc1 can now end in `error`:
- `forbid` of a tool the agent does not have. Remove the name.
- An agent that mixes `@hb.tool` with tools written by `hb.record()`. The decorated tools give it an inventory, and the funnel's tools are unknown until it calls them. Declare them with `hb.declare()`.

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
