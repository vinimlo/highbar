# Writing specs

A spec is a YAML file whose name ends in `.highbar.yaml`. `highbar run` and `highbar validate` take a file or a directory; with no argument they use every spec under `specs/`.

## Shape

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:8731/chat"
  health: "http://127.0.0.1:8731/health"

defaults:
  runs: 1

suites:
  - name: Orders
    tests:
      - name: looks the order up
        trigger: "Where is order 1042?"
        expect:
          function_calls:
            - name: get_order
              params: {order_id: "1042"}
```

- `agent` says how to reach the agent. With `start`, highbar starts it; without, it attaches to one already running. See [core concepts](/getting-started/concepts#the-agent-started-or-attached) and [attach mode](/guides/attach-mode).
- `defaults.runs` is how many times each test runs unless it says otherwise, and `defaults.min_pass` how many of those runs must pass. See [repetition](/guides/repetition).
- `suites` group tests under a name, which the output uses as a heading.

## A test

```yaml test
name: cancels after looking the order up
trigger: "Please cancel order 1042"
expect:
  ordered: true
  function_calls:
    - name: get_order
    - name: cancel_order
      params: {order_id: "1042"}
  response:
    contains: "cancelled"
```

- `trigger` is the message sent to the agent.
- `name` is optional. Without it, the output shows the trigger.
- `expect` holds the checks. Every key in it is described in [assertions](/guides/assertions).
- `turns` replaces `trigger` and `expect` for a conversation. See [multi-turn tests](/guides/multi-turn).
- `variations` asks for paraphrases of the trigger. See [variations](/guides/variations).
- `runs` and `min_pass` repeat the test. See [repetition](/guides/repetition).

## Strict on purpose

A misspelled key is the cheapest way to get a check that never runs. highbar rejects any key it does not know, at any level, and suggests the one you probably meant:

```text
  ✗ specs/quickstart.highbar.yaml: suites[0].tests[1].expect: unknown key 'orderd' — did you mean 'ordered'?
```

The same goes for a value of the wrong type, a regex that does not compile, a test whose `expect` checks nothing, and a test with both `trigger` and `turns`. Every one of them stops the run with exit code 2 before any agent starts. `highbar validate` runs the same checks without running anything.

Keys that start with `x-` are yours: highbar skips them at every level. They are a place for YAML anchors and notes:

```yaml test
x-note: "the agent must not guess an order number"
trigger: "Where is my order?"
expect:
  function_calls: []
  response:
    icontains: "order number"
```

## Templates between turns

In a multi-turn test, a trigger can use what the agent did in earlier turns, with <code v-pre>{{previous.function_calls[0].result.order_id}}</code>. Templates are resolved in triggers only. A template in `params`, `result` or `response` is a spec error, because it would be compared as literal text. See [multi-turn tests](/guides/multi-turn).
