# CLI

```console
highbar [--version] <command> [arguments]
```

## `highbar run [SPEC_PATH]`

Runs the tests of a spec file, or of every `*.highbar.yaml` under a directory. Without an argument, it uses `specs/`.

| Option | |
|---|---|
| `--runs N` | Run each phrasing N times (N ≥ 1), in place of the specs' `defaults.runs`. A test's own `runs` still wins. |
| `--frozen` | Never generate variations: a phrasing missing from the committed cache fails. Use it in CI. |
| `--keep-traces` | Keep the trace file of each spec whose agent highbar started, and print its path. |
| `--report json=FILE` | Also write the results to FILE. See the [report](/reference/report). |
| `--max-cost AMOUNT` | A ceiling on what the whole run spends. Over it, the run stops at the end of the test in progress and exits with 3. |
| `--max-errors N` | Stop the run once N executions in a row fail for the same cause of the agent's infrastructure, and exit with 4. 5 by default; 0 never stops. |

All specs are parsed and validated before any agent starts. With several specs, each one prints its own summary, and a total follows at the end. When the turns report [usage](/reference/trace-contract#usage-tokens-and-cost), a summary ends with its tokens and cost, and says `incomplete`, with what is missing, when they are only part of the sum:

```console
  2 passed | 1.31s | 3000 input tokens (800 cached) | 300 output tokens | 0.0123 USD | incomplete
  ! incomplete sum: 1 of 3 turns with no cost
```

### `--max-cost`

The accumulated cost is checked at the end of each test, across every spec of the run, so a test in progress always finishes, and the ceiling can be passed by what that test spends. Above the ceiling, not at it, the tests and specs left do not run. The agent highbar started is stopped, the results so far are kept, and the [report](/reference/report) says `stopped.reason: "budget_exceeded"`. The check also runs after the last test: a run that ended over its ceiling exits with 3, with every test in the report.

The ceiling has no currency of its own: each currency's sum is held against it separately, since costs in different currencies are not added. It sees only the costs it knows: a turn with no cost and currency cannot count, a run with one ends with a warning that it may have spent more, and an agent that reports no cost is never stopped.

A `turn` record with no test context, next to records that have one, belongs to no test, so it stays out of every total and budget. The ceiling depends on who started the agent. An agent highbar started can only be spending for this run, so the ceiling counts that cost, as it does a record tagged for no test of the run, and the message that stops the run says how much of the spend had no test context: that is why the totals can show less than what stopped it. An attached agent may be serving others, so the ceiling leaves that cost out, and the run ends with the same warning.

### `--max-errors`

When the agent's infrastructure is down, every execution ends in the same `error`, and a run with many of them would go on for as long as they take to send, at the cost of a paid model behind the agent if there is one. So highbar counts the executions in a row whose message got no reply for the same cause:

- the same HTTP status outside 2xx, a redirect included;
- a connection that could not be made;
- no response within `agent.timeout`;
- another error of the transport, such as a connection the server closed;
- an empty body.

The cause is also the endpoint: the same status from another endpoint is another cause. The body is not compared, so two 500s with different request ids in them are the same cause. At N, 5 by default, the run stops right after that execution, in the middle of a test if need be, and exits with 4. The line that says why gives the count, the cause, the endpoint and the test it stopped in:

```console
  ✗ 5 executions in a row failed with HTTP 500 from http://localhost:8000/chat (last: 'refund policy'); the infrastructure failed, not the agent, so the run stopped. Exits with 4.
```

Anything else an execution ends in starts the count over: a pass, a failure, `not observed`, a reply that came but cannot be read (not the JSON that `agent.response` names, a reply that is JSON `null`), a session hook or a template that failed, or an error of another cause. An agent that fails now and then is what [`min_pass`](/guides/repetition) is for. An execution counts once however many turns it has, with the cause of the turn that failed, and each run of each phrasing is an execution of its own. The count goes on across the tests and specs of the run.

What ran stays as it ran: the executions before the stop keep their `error`, in the terminal and in the [report](/reference/report). The test in progress is reported with the executions it had, as an `error` that says how many of its executions ran, whatever those executions were. The tests and specs after it do not run, and the agent highbar started is stopped. When the last of the N is the last execution of the last test, nothing was left out: the run still exits with 4, and the report's `complete` is `true`.

The message never shows the body of a reply, and a value from `${VAR}` in the endpoint is masked, as in every other message. `--max-errors 0` turns the stop off and runs every execution. When both stops apply, the first to come is the one reported: repeated errors stop the run in the middle of a test, and `--max-cost` is checked at its end.

## `highbar validate [SPEC_PATH]`

Parses and validates specs without running anything: unknown keys, wrong types, invalid regular expressions, tests that check nothing, and a `min_pass` above a test's runs, whether the test sets it or takes it from `defaults.min_pass`. One line per spec, `✓` or `✗` with the reason.

## `highbar init`

Writes the quickstart into the current directory: `agent.py` and `specs/quickstart.highbar.yaml`. Files that already exist are left alone. See the [quick start](/getting-started/quickstart).

## `highbar variations [SPEC_PATH]`

Generates the paraphrases that tests with `variations` ask for, and stores them in `.highbar/variations.json`. Only missing entries are generated; `--force` regenerates them all. See [variations](/guides/variations).

## Exit codes

| Code | When |
|---|---|
| 0 | Every test passed. |
| 1 | A test failed or ended in `error`. |
| 2 | Nothing could run: an invalid spec, no spec found, an agent that did not start or never wrote its trace, a port already in use, a session hook that cannot be imported. |
| 3 | `--max-cost` was exceeded, whatever the tests that ran did. The report says how each of them ended. |
| 4 | `--max-errors` executions in a row failed for the same cause of the agent's infrastructure (5 by default): the infrastructure failed, and the run says nothing about the agent. |
| 130 | Interrupted with Ctrl-C. The agent highbar started is stopped first. |
| 143, 129 | Stopped by SIGTERM or SIGHUP, the same way. |

## Environment

| Variable | |
|---|---|
| `HIGHBAR_TRACES_URL` | Set by highbar for an agent it starts: the trace file to write. |
| `HIGHBAR_LLM_API_KEY` | Key for generating variations. |
| `HIGHBAR_LLM_BASE_URL` | An OpenAI-compatible API for variations. Default `https://api.openai.com/v1`. |
| `HIGHBAR_LLM_MODEL` | The model for variations. Default `gpt-4o-mini`. |
