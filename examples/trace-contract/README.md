# An agent without the highbar package

The quickstart's support agent, with no highbar import: standard-library HTTP, and the trace file written by `emitter.py`, a reference for the [trace contract](https://vinimlo.github.io/highbar/reference/trace-contract). An agent in any language does the same: it writes one JSON line when it starts, one per tool call, and one per message it handles.

```console
pip install highbar
highbar run
```

The agent itself needs nothing beyond Python; `highbar` is only the test runner.
