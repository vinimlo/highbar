# Exporting results

highbar's verdict is the exit code and the report. To follow results over time in a tool you already use, send the report there after the run. `highbar run` itself never talks to any of them: a run is the same with or without an account, and a tool that is down never fails a test.

## Braintrust

`highbar export braintrust` reads a `--report json=` file and sends it to a [Braintrust](https://www.braintrust.dev) project as an experiment:

```bash
highbar run --report json=run.json || true
highbar export braintrust run.json --project shop-agent
```

The `|| true` lets the export run when a test failed; the report already says how the run went. The key comes from `BRAINTRUST_API_KEY`, or from the variable `--api-key-env` names. Whitespace around it, like the line ending `export KEY=$(cat file)` or a `.env` with CRLF leaves, is dropped. In GitHub Actions:

```yaml workflow
- run: highbar run --report json=run.json || true
- run: highbar export braintrust run.json --project shop-agent
  env:
    BRAINTRUST_API_KEY: ${{ secrets.BRAINTRUST_API_KEY }}
```

The experiment is named after the report file and the run's start, such as `run 2026-11-16T12:00:00.250Z`. `--experiment NAME` gives another name; an experiment of that name that is already there gets the events. A new experiment's metadata says what kind of run it holds: `complete`, `stopped`, `exit_code`, the `passed`, `failed` and `error` counts of its tests, `started_at` and `git_sha`.

### One event per execution

Each execution of each test, each phrasing and each run, is one row of the experiment:

| Field | What goes in it |
|---|---|
| `input` | The trigger, or the variation that was sent. For a conversation, the list of its messages. |
| `output` | The reply, `null` when none came. For a conversation, the list of its replies. |
| `scores.highbar` | `1` passed, `0` failed, `null` error. |
| `scores.highbar_verified` | `1` passed or failed, `0` error: whether highbar could check the execution at all. |
| `metadata` | The spec, `test_id`, suite, test name, `definition_sha256`, the phrasing's number and kind, the run, the `execution_id`, the status, the execution's errors, and `run_complete`. |
| `metrics` | Start and end, and the tokens (`prompt_tokens`, `completion_tokens`, `tokens`) and `cost` when the agent reported them. |

For example, a test that passed:

```json
{
  "id": "5f1d3c0e2a9b4e7f8c6d1a2b3c4d5e6f",
  "input": "Where is order #1?",
  "output": "It shipped.",
  "scores": {"highbar": 1, "highbar_verified": 1},
  "metadata": {
    "spec": "specs/shop.highbar.yaml",
    "test_id": "Orders/ships",
    "suite": "Orders",
    "test": "ships",
    "phrasing": 1,
    "phrasing_kind": "trigger",
    "run": 1,
    "status": "passed",
    "errors": [],
    "run_complete": true,
    "currency": "USD"
  },
  "metrics": {"prompt_tokens": 100, "completion_tokens": 10, "tokens": 110, "cost": 0.002}
}
```

### Why an error is `null`, and the second score

An `error` is a test highbar could not verify: the agent did not answer, a trace record was missing, a budget had no usage to check. Nothing was measured, so it is neither a pass nor a failure. Braintrust leaves a `null` score out of its averages; a `0` would count every execution of a run where the agent was down as a wrong answer, and the score would fall for a reason that has nothing to do with the agent's behavior. The execution's status and errors are in its metadata, to filter on.

Left out of the average, though, errors disappear from it: 1 pass and 9 errors average `1.0`, as 10 passes do. `highbar_verified` is there for that. It is `1` when highbar could judge the execution, passed or failed, and `0` for an error, so its average is the share of the run that was actually checked. Read `highbar` next to it: a `highbar` of `1.0` with a `highbar_verified` of `0.1` is one test checked, not a green agent.

A [negative control](/guides/assertions#a-test-that-must-fail-expect-status) is judged against its `expect_status`, as in the report: an execution that ended the way the control expects scores `1`. Its metadata keeps `observed_status`, what happened.

Tokens and cost are sent only when they are whole: when a sum left something out (the report's `complete: false`), the metrics are not sent and `metadata.usage_warnings` says why. Unknown is not zero. Costs in more than one currency go in `metadata.cost`, since they do not add up to one number.

### Refused reports

The export exits 2, and sends nothing, for a file that is not a highbar JSON report, a `schema_version` this highbar does not read, and a run that did not finish (`exit_code: null`): a run that was killed has part of its tests, and its experiment would look like a whole one.

A run that ended before every test ran (`complete: false`) is refused the same way, with what stopped it: `--max-errors` when the agent's infrastructure failed, `--max-cost`, Ctrl-C. The tests that never ran are missing from the experiment, and the ones that ended in `error` are out of its `highbar` average, so a run where the agent went down after its first test would read as 100%. `--partial` sends it anyway, with `complete: false` and `stopped` in the experiment's metadata and `run_complete: false` on each event, and the summary line says it is partial. A run that did not finish is refused even with `--partial`.

A run of part of the tests, whose [`--suite` or `-k`](/reference/cli#part-of-the-tests) left tests out, is `complete: false` too, and is refused the same way, with the report's `selection`: `part of the tests: 1 of 3 picked by -k looks, 2 left out`. Its experiment would show the tests picked as the whole suite. `--partial` sends it, and the summary line says it was part of the tests.

A missing key, or one that is empty or not printable ASCII once the whitespace around it is dropped, is exit 2 too, with the variable's name. The key never appears in any message, and nothing the export does ends in a traceback.

### When Braintrust fails

A `429`, a `5xx` or a request with no answer is tried again, three times, with a wait that doubles. When a request still fails, the export exits 1 and says how many events were sent and how many were not. Sending the same report again is safe: each event's id comes from the run and the execution's place in it, and Braintrust replaces a row with the same id, so nothing is counted twice.

`--api-url` points at another Braintrust API, such as `https://api-eu.braintrust.dev` or your own deployment's.
