# Quickstart

A support agent for an online store, with no LLM, and the spec that tests it. `highbar init` writes these two files into the current directory; this is where they come from.

```console
pip install "highbar[quickstart]"
highbar init
highbar run
```

`agent.py` is a FastAPI app whose tools are recorded with `@hb.tool`. `specs/quickstart.highbar.yaml` starts it, sends four messages, and checks the tool calls and the replies. To watch the guardrail tests fail, set `GUARDRAILS = False` in `agent.py` and run `highbar run` again.
