# Troubleshooting

Every message below is copied from a real run. The first line of each tells you what happened; the rest says what to do.

## The run stops before any test (exit code 2)

**The port is taken.**

```text
  ✗ specs/quickstart.highbar.yaml: could not start the agent: port 8731 is already in use by Python (pid 2152). Stop it first: tests would talk to it instead of the agent highbar starts.
```

highbar refuses to start the agent on a busy port, because the tests would reach whatever is listening there. Stop that process, or change the port in `start`, `endpoint` and `health`.

**The agent dies while starting.** highbar shows the end of the agent's log:

```text
  ✗ specs/dies.highbar.yaml: could not start the agent: agent exited with code 1 before it was ready
--- last lines of .highbar/logs/dies-20261026-001531.log ---
Traceback (most recent call last):
ModuleNotFoundError: No module named 'fastapi'
```

The whole log stays in `.highbar/logs/`. When a run has failures, the last line of the output names it too.

**The agent answers but writes no trace.**

```text
  ✗ specs/untraced.highbar.yaml: the agent answered, but nothing was written to /var/folders/yy/jp01z9wn1zl6d_qzyyb5z5v40000gn/T/highbar-traces-55u9g9a8.jsonl: another process on the port, or Highbar() was never created (it reads HIGHBAR_TRACES_URL when constructed)
```

The agent must create `highbar.Highbar()` after `HIGHBAR_TRACES_URL` is in its environment. A start script that resets the environment, or a `Highbar(traces=...)` pointing elsewhere, gives this message.

**The spec is invalid.** The message names the key and the likely fix: `unknown key 'orderd' — did you mean 'ordered'?`. Run `highbar validate` to check every spec without running anything.

## A test is `error`, not `failed`

`error` means highbar could not check the behavior. It is never a pass, and it fails the run like `failed` does, but it points at the setup rather than at the agent.

- **`not observed`**: no trace record shows the agent handled this message. In attach mode the message says whether nothing was written, the records had no context, or they had context without the keys of `agent.correlate`. See [attach mode](/guides/attach-mode#which-records-belong-to-a-test).
- **`cannot tell which test made get_order`**: a call with no test id would decide the result. The call was made outside the request's context, typically in a thread the agent started. Start it with `contextvars.copy_context().run(...)`, or pass the tag to `hb.record()`.
- **`HTTP 307 … redirected to …`**, **`no response in 60s (agent.timeout)`**, **an empty reply**: the request did not get an answer to check. Fix the endpoint, or raise `agent.timeout`.
- **`session.setup …`**: the session hook failed, and the message is the hook's own.

## A warning about calls without a test context

```text
  ! some tool calls carry no test context, so each counts for the test whose window it falls in, and a call that ends after the response can land in the next test. highbar.asgi.Middleware or hb.turn(test=...) tags them; a thread the agent starts itself needs contextvars.copy_context() to keep the tag
```

The tests still run, but a slow call can be counted for the wrong test. Add `highbar.asgi.Middleware`, or wrap message handling in `hb.turn()`. See [connecting your agent](/guides/connecting-your-agent#tag-each-message-with-its-test).

## A call recorded after the test's window closed

highbar reads the trace file `settle_time` milliseconds (500 by default) after the reply arrives. A tool call that ends later, such as a background task, is still found when the next test reads the file: it fails the run it belongs to, with a message that says it was recorded after the window closed. Raise `agent.settle_time` when the agent legitimately finishes work after it replies.

## Looking at the trace file

`highbar run --keep-traces` keeps the file highbar created for a started agent and prints its path. Each line is one record of the [trace contract](/reference/trace-contract).
