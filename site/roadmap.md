# Roadmap

highbar 0.1 does what the rest of this site describes. These are the things it deliberately does not do yet, and might next. There are no dates.

- **Tool stubs.** Replacing a tool's result in a test, so the agent can be tested against a failure without breaking a real system. Today, a clean state per test comes from session hooks.
- **Machine-readable reports.** JSON and JUnit output for CI dashboards. Today the gate is the exit code.
- **Variations in multi-turn tests.**
- **A separate `llm` record.** One trace record per model call, with its own tokens and cost, next to the `usage` of a `turn`, and a way to correlate the two so nothing is counted twice. Today the sums read `usage` from `turn` records alone, and a line with `"type": "llm"` is skipped like any other type highbar does not know.
- **SDKs in other languages.** The [trace contract](/reference/trace-contract) is the stable interface; an SDK is a convenience over it.
- **LLM-graded checks, kept apart.** A judge for the quality of an answer, as its own kind of check. The deterministic verdict stays what it is: a spec without a judge never needs a model, and a judge never changes what a deterministic assertion decided.

For graded quality today, see the [comparison](/comparison).

Ideas and requests: [GitHub issues](https://github.com/vinimlo/highbar/issues).
