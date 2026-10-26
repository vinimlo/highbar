# Multi-turn tests

A conversation is a test with `turns`. Each turn has its own trigger and expectations, and the turns run in order against the same agent, in the same session:

```yaml test
name: cancels the order the customer just looked up
turns:
  - name: looks it up
    trigger: "Where is order 1042?"
    expect:
      function_calls:
        - name: get_order
          params: {order_id: "1042"}
  - name: cancels that one
    trigger: "Cancel it, please"
    expect:
      function_calls:
        - name: cancel_order
          params: {order_id: "1042"}
```

- The turns share one test execution: one test id and one session, so an agent that keeps memory per conversation sees a conversation.
- The test stops at the first turn that does not pass. Later turns build on that one's state, and running them would test a conversation that did not happen.
- A failure says which turn broke, with its name or trigger: `turn 2/2 (cancels that one): Expected call to 'cancel_order', but the agent called: get_order`.

## Using what an earlier turn returned

A trigger can carry a value from an earlier turn's tool calls:

```yaml test
turns:
  - trigger: "Find my last order"
    expect:
      function_calls:
        - name: find_last_order
  - trigger: "Cancel order {{previous.function_calls[0].result.order_id}}"
    expect:
      function_calls:
        - name: cancel_order
```

<code v-pre>{{previous.function_calls[N]}}</code> is the Nth call of the earlier turns, counted from the first, with the fields `fn`, `args`, `result`, `error`, `started_at`, `ended_at` and `context`. A text value goes in as it is; a number, list or object goes in as JSON. Spaces inside the braces are allowed.

A path that does not resolve makes that test an `error` that names the path and what was there instead, and the rest of the run goes on.

Variations do not apply to multi-turn tests.
