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

## Quoting what the agent said

A trigger can quote the agent's reply, the way a customer comes back to "that second one you said":

```yaml test
turns:
  - trigger: "Which plans do you have?"
    expect:
      response: {icontains: "pro"}
  - trigger: 'You said: "{{previous.response}}". Which of those is the cheapest?'
    expect:
      response: {icontains: "basic"}
```

<code v-pre>{{previous.response}}</code> is the reply of the turn right before, and <code v-pre>{{previous.turns[N].response}}</code> the reply of the Nth earlier turn, counted from 0. The reply goes in whole, as `agent.response` read it. A path ends on `.response`: a turn, or the list of turns, is not a value.

A path that does not resolve makes that test an `error` that names the path and what was there instead, and the rest of the run goes on. So does a reply that was not read: the turn is an `error`, and the text `null` is never sent. In the first turn there is no previous response.

Variations do not apply to multi-turn tests.
