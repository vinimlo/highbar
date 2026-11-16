# Changelog

## Unreleased

### Added

- `variations` on the last turn of a multi-turn test: each paraphrase replaces only that turn's trigger, and the turns before it are sent as written. The paraphrases are generated with the earlier turns' messages, and cached apart from the same sentence in another conversation; a single-turn test's cache key does not change. `variations` on an earlier turn, or on the test, does not load, and neither does a last turn whose trigger has a template.
- A test's `history` can go in the request, for an agent with no state of its own that takes the whole conversation each time: `{{test.history}}` in `agent.request` hands it on in a single-turn test, with no session. A conversation of two turns or more still needs the session's setup, and the spec says why.
- A session hook over HTTP: `setup` and `teardown` take `{http: {url, method, headers, body}}`, with placeholders in each part. highbar sends the request itself, so it can run from an image of its own instead of living in the agent's to import its code. A setup's reply must be a JSON object, which becomes `{{session.*}}`; a reply that is not 2xx, a redirect included, or no reply within `agent.timeout`, is the hook's failure. In the URL, placeholders go in the path and the query, and each value is one percent-encoded piece, so a session value cannot move the request to another resource or host.
- A test's own session arguments: `session: {args: {...}}` under a test, a JSON object its hooks receive (`ctx.args` in Python) and that `{{test.args.<path>}}` reads in a command, an HTTP hook, the request and `correlate`. Each one the agent uses must have a value in every test, checked when the spec loads. A trigger does not read them, and `{{test.args.<path>}}` in one stops the spec, since it would go to the agent as written. The same args can go in a suite and in `defaults`: the most specific level wins key by key, and a key is replaced whole.
- The `llm` trace record, one per call to the model (`hb.llm(...)` in Python), and `expect.llm` to check the test's calls: `model` (the call that answered), `fallback`, `last_role` (every call) and `max_calls`. A test with no `llm` record is an `error`, and a late one judges its own test again. Its tokens are not summed: the `turn`'s `usage` stays the count.
- `returns` in a `function_calls` entry: the whole value the tool returned, as any expected value (a matcher, a list, a text). A tool that returns a list or a text could not be checked, since the top of `result` is key names, as in `params`: `returns: {len_lte: 3}` is "at most 3 documents".
- The `retrieval` trace record, what entered the prompt for a message (`hb.retrieval(...)` in Python), and `expect.retrieval`: `includes` (by id or title, and `source`), `excludes`, `max_items`, `max_tokens` and `chunks_in_order`. It is not a tool call. A test with no `retrieval` record is an `error`, and a late one judges its own test again.
- `highbar compare --base … --head …`: two groups of `--report json=` runs, one round per file, compared test by test with Fisher's exact test, a Wilson interval per side and Holm across the tests. A test is `better` or `worse` only with a Holm-adjusted p under 0.05, at least 2 rounds a side and rounds that agree with each other; otherwise the output says why there is no verdict. A test whose definition changed is not compared, `error` executions count as not passed, but never make the head look better, and a report given twice is refused. `--fail-on-regression` exits 1 only on `worse`.
- A test that starts in the middle of a conversation: `history`, a list of `{role, content}`, is handed to the session's setup to seed (`ctx.history` in Python, `{{test.history}}` as JSON in a command, an HTTP hook or the request), and only the test's own turns run and are checked. A test with a `history` needs something that hands it on: a Python setup, a command or HTTP one that uses `{{test.history}}`, or, in a single-turn test, the request.
- `agent.traces` takes a glob (`var/traces/*.jsonl`): one trace file per process of an attached agent, read together in time order, so a server and its workers no longer share a file whose appends a bind mount may cut. The SDK puts the process id where the path says `{pid}`. Clear the trace folder before each start of the agent, so an old process's file does not vouch for its tools; the SDK also writes a `bye` record when Python itself ends the process (the end of its main module, `sys.exit`, Ctrl-C; not SIGTERM), and the tools of a process that wrote one stop counting. A glob that matches no file stops the run before the first message, as a missing file does. A link to a file is read once, and a path that names a file is that file.
- Tokens and cost. A `turn` record can carry an optional `usage`: `model`, `provider`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost`, `currency` and `llm_calls`, all optional. The contract stays at v1, and a writer without it works as before. In Python, set `turn.usage` in `hb.turn()`; a `Decimal` in it is written as a number.
- A price table for the cost a writer does not send: `src/highbar/engine/prices.json`, in USD per token, in the format of OpenRouter's model list, rewritten by `scripts/update_prices.py`. A cost the writer sends always stands. A model outside the table leaves the cost missing, with a warning.
- Each turn of the report has its `usage`, and each execution, test, suite and spec, and the run, have `totals`. A sum holds only what is known: when a cost or a currency is missing, or the costs come in more than one currency, `complete` is `false` and `warnings` says what is missing. Costs in different currencies are never added together. The terminal's summary ends with the tokens and cost.
- `expect.budget`, with `max_input_tokens` and `max_cost`: per turn in a turn's `expect`, and for the whole execution in a test's. A multi-turn test can now have an `expect` that holds only a `budget`. `defaults.budget` applies to the tests without their own. Over a limit, the run fails and counts against `min_pass`; a budget without the usage to check it is an `error`. Usage follows the rules of tool calls: a `turn` with no test context counts for no test, and a budget that holds only without it is an `error`; a `turn` recorded late counts for its test, which is judged again. When nothing in a window is tagged, its usage counts for that window's test, and the test before it is an `error` if its budget holds only without that usage: an agent that tags nothing gets this whenever two neighbouring tests together pass the limit, and tagging fixes it.
- `{{previous.response}}` and `{{previous.turns[N].response}}` in a trigger: a turn quotes the agent's reply, the one right before or the Nth earlier one. A reply that was not read makes the turn an `error`, and `null` is never sent.
- `{{session.<path>}}` in a trigger, as in the request: a value the session's setup returned. It needs `agent.session`, and a test whose trigger uses it cannot have `variations`, which could drop or change it. A missing key makes the test an `error`, and nothing is sent.
- `highbar run --max-cost AMOUNT`: a ceiling on what the whole run spends. Over it, the run stops at the end of the test in progress, keeps its results, reports `stopped.reason: "budget_exceeded"` and exits with the new code 3. When the ceiling is passed only at the last test, or by usage read after it, no test was left out, and the report's `complete` stays `true`. With an agent highbar started, the ceiling also counts the cost of a `turn` with no test context, which is in no total, and the message that stops the run says how much of the spend that was.
- `imatches` and `not_imatches`: `matches` and `not_matches`, ignoring case, in `response`, `params` and `result`.
- `lt`, `lte`, `gt` and `gte`, on numbers, and `len` and `len_lte`, on the length of a list, in values of `params` and `result`: `limit: {gte: 1, lte: 3}`, `documents: {len_lte: 3}`. A bound fails on a value that is not a number, a numeric string and a boolean included, and a length on a value that is not a list. Operands are checked when the spec loads, and `response` and `error`, which are text, refuse them.
- `in` (one of a list of values), `contains_all` (`contains` for each listed item), and `any` and `all`, which match the items of a list with any expected value: `documents: {any: {id: "policy-7", chunks: [0, 1, 2]}}`. `all` fails on an empty list, where nothing was checked. Like the bounds, they are for values of `params` and `result`.
- `defaults.min_pass`: the threshold for every test without its own `min_pass`, stated once instead of in each test. It takes the same values as the test's: a whole number of runs, or a fraction of each test's runs, rounded up and computed again with `--runs`. A test's own `min_pass` still wins; without either, every run must pass, as before. A whole number above a test's runs is an error in `validate` and before `run` starts the agent, naming the test and `defaults.min_pass`.
- `highbar run --report json=<file>`: the results as JSON, with one test, phrasing, execution and turn per entry, in their final state. The file is replaced before the first message and written on every way out of the run, with the code the process returns: a run that dies leaves `exit_code: null`, never an older green. See the report reference.
- An optional `id` on a test, and a stable key for each one: its `id`, or `<suite>/<name>`.
- `definition_sha256` on each test of the report, to tell whether two runs measured the same thing.
- `highbar.run()` returns each test in `summary.tests` and the warnings in `summary.warnings`, and takes a `reporter`.
- `highbar run --report junit=<file>`: the report as JUnit XML, for the CI tools that read it, with the JSON report's lifecycle. Each spec is a `testsuite` and each test a `testcase`, with a `failure` or an `error`. A run that did not finish, stopped halfway, or exited with an error code while no test failed gets one more case, `highbar.run`, with an `error`: zero failed cases would read as a pass.
- `highbar run --report markdown=<file>`: the report as a Markdown summary for a CI job's page (`$GITHUB_STEP_SUMMARY`): whether the run passed, the tests that did not with why, and the others folded. Text from the agent is escaped, and a long list is cut, so the page stays under GitHub's 1 MiB.
- `--report` can be given more than once, one file per format, all from the same run.
- `expect_status: failed` or `expect_status: error` on a test, for a negative control: the test passes when it ends exactly in that state, and fails when it passes or ends in the other one, so a control that works keeps the run green. The terminal marks it `(expected failed)`, and the report has `expect_status` on each test, with `status` judged against it.
- `agent.run`: a `setup` and a `teardown` that run once per spec, around the run, to change something before it and put it back after it, like a feature flag on a shared bot. The `setup` runs before the agent starts, and one that fails stops the run with exit code 2 before anything is sent. The `teardown` runs on every way out once the `setup` finished, Ctrl-C and SIGTERM included, and one that fails is a warning. Each is a command, a Python function or an HTTP request, like a session hook, with no test's placeholder. What the `setup` returns is `{{run.<path>}}`, for the request, `correlate`, the session hooks and the `teardown`, which can put back what the `setup` found; a spec that reads it needs a `setup` that returns a JSON object. A trigger, `agent.start` and `agent.traces` do not read it, and a Python session hook's `ctx.run` of a `setup` that returned no JSON object is an error when read.
- `expect.check: {python: "module:function"}`, a check of your own, for an outcome that is in neither the reply nor a tool call, like a row the agent must have written. The function gets the turn (the message, the reply, the session, the test's args and history, and the trace records highbar counted as the turn's) and returns its failures: none is a pass. Anything else, or an exception, leaves the turn `error`, and so does reading the trace records of a turn highbar did not observe. It runs once per execution, after highbar's own checks, in the thread of the session hooks.
- A Ctrl-C, SIGTERM or SIGHUP that lands while `agent.run`'s `teardown` runs waits for its end, within `agent.timeout`, and then stops highbar as it would have: a run that ended, or a second Ctrl-C, no longer leaves what the `setup` changed in place.
- The wait after a Ctrl-C, SIGTERM or SIGHUP during `agent.run`'s `teardown` says why: "Waiting for agent.run.teardown to put back what agent.run.setup changed (at most 60s, agent.timeout), then stopping.", once, on the first signal. It was silent, and read as a hang.

### Changed

- What a Python session hook prints, and what the logging handlers its module sets up at import write, goes to `.highbar/logs/<spec>-hooks-<timestamp>.log` instead of into the run's output, so a setup that imports a whole application no longer fills the terminal with its INFO lines. When the hook raises or outlives `agent.timeout`, its error names the file and ends with the last 10 lines it wrote.
- Every run waits `agent.settle_time` (500 ms by default) after its last test and reads the trace file once more, also when its spec checks only replies, so a `turn` recorded late still counts in the totals, in budgets and in `--max-cost`. A spec that checks only replies takes that much longer than in 0.1.0.
- A `{{session.<path>}}` whose value is `null`, in the request, `correlate` or a session hook, is an `error` that says the setup returned no value for it, instead of going out as `null`.
- A value from `${VAR}` is masked by where it goes. It stays masked in `agent.request`, in `agent.session` (every hook, a command line included) and in a URL's password, query and fragment. The URL is read as the spec wrote it and as it is sent: a variable before an `@` or after a `?` or `#` in the spec's text is masked whatever its value holds, and so is one whose value lands in those parts once the values are in; a value with an `@`, or a URL that is not `scheme://host` or has a space, a control character or a backslash, has every variable of the URL masked. It is now shown in `agent.traces`, `agent.start`, `agent.correlate` and a URL's host, port and path, so a trace file that does not exist is named in full, a relative path too, and one variable can hold the traces path for the compose file, the spec and the Makefile. A value masked in one place is still masked everywhere it appears. A token passed on `start` or in a URL's path is now shown: pass it in the environment or in a header.
- A failure repeated across a phrasing's runs is one line, with how many runs it hit, also when what the agent did changed between them, its reply or the calls it made: the line is the first run it failed in, `(3 of 5 runs; as in run 2)`. Before, each reply made its own line, so `runs: 5` with two matchers that failed gave ten. The JSON report keeps each run's own message.

### Fixed

- An HTTP session or `agent.run` hook stops at `agent.timeout` when its server sends the status line and headers slowly. httpx's timeout holds for each read, so a server that sent a byte every 0.1 s kept a hook of 0.5 s waiting for 12 s.
- A guardrail no longer passes when the agent highbar started died and another process answered on its port. The port is checked free before the start, but a process that took it before the agent's own bind answered every message while the agent's trace file stayed empty, and in managed mode an empty window with nothing tagged is the test's. Once highbar sees the agent gone with something else on its port, each execution with no record of its own is an `error`, including the ones already reported. With nothing on its port, the agent crashed: what it answered stands, and a warning says so. An agent gone right after its hello stops the run with exit code 2.
- A reader that leaves right after the summary line (`| sed '/passed/q'`) no longer turns a passing run's exit code into 1, now and then. The blank line after the summary, and with `--keep-traces` the line with the trace file's path, went in writes of their own, and when the reader had already left, such a write found the pipe closed. The summary, its warnings and that blank line now go in one write, and the trace file's path is printed before the run.
- A trace line that cannot be read no longer throws away the lines read with it. The test that read it is still an `error`. The other records count as in any read: a late call or a late `turn` among them still fails the test it belongs to, and their usage is in the sums, in `--max-cost` and in budgets. The line may be the late usage of the test before, so that test is an `error` too if it passed and has a budget.
- A value from `${VAR}` is masked in every spec of a run, not only in the spec that uses it, and when a message is cut in the middle of it. Two or fewer of its first characters can still show at a cut: telling a shorter start from an ordinary word is not possible.
- `agent.traces` pointing to a directory, which Docker leaves when a bind mount names a file that does not exist yet, stops the run before the first message with a message that says so, and exit code 2, instead of a traceback. A trace file highbar cannot read for another reason is named the same way.
- An `endpoint` or `health` that Python cannot split, with a `[` or `]` that is not a pair around an IP address (`http://[::1:8000/chat`, a bracket in a password) or a character that reads as `/ ? # @ :` once normalized (a fullwidth `／`), is a spec error that names the key and says why, exit 2, instead of a traceback with exit 1. That traceback quoted the host part, and with it a password; the spec error masks a value from `${VAR}` like every other.

### Upgrading from 0.1.0

- An empty text in a matcher of `params`, `result` or `error` (`contains: ""`, `matches: ""`, and their `i` and `not_` forms) no longer loads: the positive ones held on any value, the negative ones on none. `response` already refused them. Write `{equals: ""}` to compare with an empty text.
- An object in a value of `params` or `result` whose keys are all among `lt`, `lte`, `gt`, `gte`, `len`, `len_lte`, `in`, `contains_all`, `any` and `all` (with the older matcher names) is now a matcher, not a literal to compare. To compare such an object as it is, wrap it: `{equals: {lt: 5}}`. The keys right under `params` and `result` are always argument names, so an argument called `gt`, `len` or `in` is compared as before.
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
