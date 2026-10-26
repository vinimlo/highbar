# Roadmap

highbar 0.1 does what the rest of this site describes. These are the things it deliberately does not do yet, and might next. There are no dates.

- **Tool stubs.** Replacing a tool's result in a test, so the agent can be tested against a failure without breaking a real system. Today, a clean state per test comes from session hooks.
- **Machine-readable reports.** JSON and JUnit output for CI dashboards. Today the gate is the exit code.
- **Variations in multi-turn tests.**
- **SDKs in other languages.** The [trace contract](/reference/trace-contract) is the stable interface; an SDK is a convenience over it.

highbar will not grade answers with an LLM. Its verdicts are deterministic, so the same behavior always gives the same result; for graded quality, see the [comparison](/comparison).

Ideas and requests: [GitHub issues](https://github.com/vinimlo/highbar/issues).
