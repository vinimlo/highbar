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

All specs are parsed and validated before any agent starts. With several specs, each one prints its own summary, and a total follows at the end.

## `highbar validate [SPEC_PATH]`

Parses and validates specs without running anything: unknown keys, wrong types, invalid regular expressions, tests that check nothing, and a `min_pass` above the spec's runs. One line per spec, `✓` or `✗` with the reason.

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
| 130 | Interrupted with Ctrl-C. The agent highbar started is stopped first. |
| 143, 129 | Stopped by SIGTERM or SIGHUP, the same way. |

## Environment

| Variable | |
|---|---|
| `HIGHBAR_TRACES_URL` | Set by highbar for an agent it starts: the trace file to write. |
| `HIGHBAR_LLM_API_KEY` | Key for generating variations. |
| `HIGHBAR_LLM_BASE_URL` | An OpenAI-compatible API for variations. Default `https://api.openai.com/v1`. |
| `HIGHBAR_LLM_MODEL` | The model for variations. Default `gpt-4o-mini`. |
