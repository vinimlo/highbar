# Python API

```python
import highbar

summary = highbar.run("specs/quickstart.highbar.yaml", runs=3)
if summary.failed or summary.errored:
    raise SystemExit(1)
```

## `highbar.run(spec, *, frozen=False, keep_traces=False, runs=None, reporter=None)`

Runs one spec file the way `highbar run` does, prints the same report, and returns a summary.

- `spec`: the path to a `.highbar.yaml` file.
- `frozen`: never generate variations (`--frozen`).
- `keep_traces`: keep the trace file of an agent highbar started (`--keep-traces`).
- `runs`: in place of the spec's `defaults.runs` (`--runs`); a test's own `runs` still wins. A whole number of at least 1.
- `reporter`: receives each test as it ends, in place of the terminal output. It has the methods of `highbar.domain.protocols.Reporter`. A late call can change a test after `on_test_result`; `on_run_end` gets the final state.

An agent highbar started is stopped before `run` returns or raises.

### The summary

| Field | |
|---|---|
| `total` | Tests run. |
| `passed`, `failed`, `errored` | Tests in each state. |
| `duration_ms` | Wall time of the run. |
| `variations` | Paraphrases that ran, each counted once. |
| `tests` | Each test in its final state: the fields of the [report](/reference/report#each-test), as dataclasses. Values from `${VAR}` are masked. |
| `warnings` | The warnings of the run. |

### Errors

A run that cannot start raises instead of returning:

- `highbar.engine.schema.SpecError`: the spec is invalid, or a `min_pass` is above its runs;
- `highbar.engine.session.SessionError`: a session hook cannot be loaded;
- `highbar.adapters.subprocess_adapter.AgentStartError`: the agent did not come up;
- `highbar.engine.observation.AgentNotObservedError`: the agent highbar started answered but wrote nothing to its trace file, or an attached agent's trace file does not exist;
- `ValueError`: `runs` is not a whole number of at least 1.

`highbar.run` is loaded on first use, so `import highbar` in an agent does not pull in the CLI's dependencies.
