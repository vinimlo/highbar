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

All specs are parsed and validated before any agent starts. With several specs, each one prints its own summary, and a total follows at the end. When the turns report [usage](/reference/trace-contract#usage-tokens-and-cost), a summary ends with its tokens and cost, and says `incomplete`, with what is missing, when they are only part of the sum:

```console
  2 passed | 1.31s | 3000 input tokens (800 cached) | 300 output tokens | 0.0123 USD | incomplete
  ! incomplete sum: 1 of 3 turns with no cost
```

### `--max-cost`

The accumulated cost is checked at the end of each test, across every spec of the run, so a test in progress always finishes, and the ceiling can be passed by what that test spends. Above the ceiling, not at it, the tests and specs left do not run. The agent highbar started is stopped, the results so far are kept, and the [report](/reference/report) says `stopped.reason: "budget_exceeded"`. The check also runs after the last test: a run that ended over its ceiling exits with 3, with every test in the report.

The ceiling has no currency of its own: each currency's sum is held against it separately, since costs in different currencies are not added. It sees only the costs it knows: a turn with no cost and currency cannot count, a run with one ends with a warning that it may have spent more, and an agent that reports no cost is never stopped.

A `turn` record with no test context, next to records that have one, belongs to no test, so it stays out of every total and budget. The ceiling depends on who started the agent. An agent highbar started can only be spending for this run, so the ceiling counts that cost, and the message that stops the run says how much of the spend had no test context: that is why the totals can show less than what stopped it. An attached agent may be serving others, so the ceiling leaves that cost out, and the run ends with the same warning.

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
| 130 | Interrupted with Ctrl-C. The agent highbar started is stopped first. |
| 143, 129 | Stopped by SIGTERM or SIGHUP, the same way. |

## Environment

| Variable | |
|---|---|
| `HIGHBAR_TRACES_URL` | Set by highbar for an agent it starts: the trace file to write. |
| `HIGHBAR_LLM_API_KEY` | Key for generating variations. |
| `HIGHBAR_LLM_BASE_URL` | An OpenAI-compatible API for variations. Default `https://api.openai.com/v1`. |
| `HIGHBAR_LLM_MODEL` | The model for variations. Default `gpt-4o-mini`. |
