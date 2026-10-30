# Report

`highbar run --report json=<file>` writes the results to a file as well as to the terminal, so a run can be compared with another, kept as a CI artifact or read by a script, without parsing the output.

```console
highbar run --report json=.highbar/reports/run.json
```

One file holds every spec of the run. The schema is [`report.schema.json`](https://github.com/vinimlo/highbar/blob/main/src/highbar/reporters/report.schema.json), shipped in the package.

```console
jq -r '.specs[].tests[] | [.id, .counts.passed, .counts.failed, .counts.error] | @tsv' run.json
```

::: warning The file holds what the agent said
Responses and tool arguments go in whole, so treat it like a trace file from `--keep-traces`. Values from `${VAR}` are masked everywhere, the same way as in the output. A tool's `result` and `context` have no field of their own, but a `result` can still get in: a failed check quotes it in `errors`, and a <code v-pre>{{previous.*}}</code> template puts it in `turns[].trigger`. The session's values appear where `agent.correlate` uses them.
:::

## What the file says about the run

| Field | |
|---|---|
| `schema_version` | `1`. It changes only on a breaking change. New fields can appear within a version, so a reader ignores what it does not know. |
| `highbar_version` | |
| `started_at`, `duration_ms` | UTC with milliseconds; wall time of the run. |
| `exit_code` | The code the process returned. `null` means the run did not finish: it is still going, or it was killed. |
| `complete` | Every spec ran to its end. |
| `stopped` | Why the run ended early, or `null`: `{"reason": ..., "message": ...}`. |
| `git_sha` | `HEAD` where highbar ran, which is not always the agent's version. `null` outside a repository. |
| `summary` | `total`, `passed`, `failed`, `errored` and `variations` of the whole run. |
| `specs` | One entry per spec that started. |

`stopped.reason` is one of `spec_invalid`, `hooks_invalid`, `no_specs`, `agent_start_failed`, `agent_not_observed`, `interrupted` and `crashed`.

The file is written before the first message and again on every way out of the run, through a temporary file and a rename. So it is never half written, and it never shows an older run:

- A path that cannot be written stops the run with exit code 2 before anything is sent.
- A run that was killed leaves `exit_code: null`.
- An interrupted run keeps the tests that finished, and the test in progress is left out.
- A run that cannot write the final file exits with 2, whatever its tests did.

## Each spec

| Field | |
|---|---|
| `key` | The spec's path inside its repository. Outside a repository, the path as given. |
| `path` | As given on the command line. |
| `sha256` | Of the file, read before the first test. |
| `complete`, `summary`, `warnings` | As above, for this spec. |
| `tests` | The tests that finished. |

## Each test

```json
{
  "id": "Orders/looks the order up",
  "suite": "Orders",
  "name": "looks the order up",
  "definition_sha256": "d54e20d9f07f962144ce7e6892c018a3b1b940b00d8bd6860350fa418c909ba1",
  "status": "passed",
  "runs": 1,
  "min_pass": 1,
  "counts": {"passed": 1, "failed": 0, "error": 0},
  "duration_ms": 652.0,
  "errors": [],
  "phrasings": [
    {
      "kind": "trigger",
      "text": "Where is order 1042?",
      "status": "passed",
      "counts": {"passed": 1, "failed": 0, "error": 0},
      "executions": [
        {
          "execution_id": "066328e340184d0dbe2a63aa92e35e95",
          "run": 1,
          "started_at": "2026-10-30T15:56:08.739Z",
          "status": "passed",
          "duration_ms": 652.0,
          "errors": [],
          "turns": [
            {
              "trigger": "Where is order 1042?",
              "response": "Order 1042 is in transit, arriving Thursday.",
              "response_ms": 3.5,
              "correlate": {"test": "066328e340184d0dbe2a63aa92e35e95"},
              "tools": [
                {
                  "name": "get_order",
                  "args": {"order_id": "1042"},
                  "error": null,
                  "duration_ms": 0.1,
                  "late": false
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

| Field | |
|---|---|
| `id` | The test's [`id`](/reference/spec-format#tests) in the spec, or `<suite>/<name>`. Unique in a spec. |
| `definition_sha256` | Of the trigger, the turns, the expectation and the number of variations. `name`, `id`, `runs` and `min_pass` stay out. |
| `status`, `errors` | The final ones, late calls included. |
| `runs`, `min_pass` | As they applied to this test, with fractions resolved. |
| `counts` | How the executions ended. There is no rate: an `error` is neither a pass nor a failure, so whoever computes one has to decide what to do with it. |
| `phrasings[].kind` | `trigger`, `variation`, or `conversation` for a multi-turn test, whose `text` is `null`. A test that stopped before running, such as one without its variations, has no phrasings. |
| `executions[].execution_id` | The `X-Highbar-Test` value of this execution. |
| `duration_ms` | Includes `agent.settle_time`. |
| `turns[]` | The turns that ran: a conversation stops at the first one that does not pass. |
| `turns[].response` | `null` when no reply came, which is not the same as `""`. |
| `turns[].response_ms` | The send alone, without `agent.settle_time`. |
| `turns[].correlate` | The values of `agent.correlate` for this turn: what finds its records in the trace file. |
| `tools[].late` | Recorded after the test's window closed. |

## Comparing runs

A test is the pair `(specs[].key, tests[].id)`: the same `id` can appear in two specs.

Before putting two results together, compare `definition_sha256`: equal hashes measured the same thing. For a test with `variations`, the hash holds how many it asks for and not their text, so compare `phrasings[].text` too. A changed `sha256` of the spec says only that something in the file changed.

Keep one report per run, and compare them as groups. Two rounds of an LLM agent can differ with nothing changed: when it chooses between two near-identical passages, the choice can hold within a round and flip in the next one. In that case 5 runs are not 5 independent observations, and a test of proportions over the executions reports a change that did not happen. Several short rounds show that variation, and one long round hides it.
