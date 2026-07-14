# Multi-Turn Testing

Multi-turn tests simulate conversations that span multiple exchanges between the user and the agent. They let you verify that your agent handles stateful interactions correctly and that values from earlier turns flow into later ones.

## Basic Structure

Instead of a single `trigger`/`expect` pair, multi-turn tests use a `turns` array. Each turn has its own `trigger` and `expect`:

```yaml
tests:
  - turns:
      - trigger: "Find my recent orders"
        expect:
          function_calls:
            - name: list_orders

      - trigger: "Cancel the first one"
        expect:
          function_calls:
            - name: cancel_order
```

## Template Syntax

The <code v-pre>{{previous.*}}</code> syntax lets you reference values from traces captured in earlier turns. This is essential for tests where the second message depends on data returned by a tool in the first turn.

### Syntax

```
&#123;&#123;previous.function_calls[N].field&#125;&#125;
```

Where:
- `function_calls[N]` selects the Nth trace (zero-indexed) from all previous turns
- `field` is any field on the trace: `fn`, `args`, `result`, `started_at`, `ended_at`, `error`

### Supported Paths

| Path | Resolves To |
|------|-------------|
| `function_calls[0].fn` | Function name of the first trace |
| `function_calls[0].args.order_id` | The `order_id` argument of the first call |
| `function_calls[0].result.order_id` | The `order_id` field in the return value |
| `function_calls[0].result.items[0]` | Not supported -- only dot-path navigation into dicts |
| `function_calls[1].fn` | Function name of the second trace |

### How Resolution Works

The template engine tokenizes the path by splitting on `.` and extracting bracket indices. It then navigates the data structure step by step:

1. `function_calls` maps to the list of all Trace objects from previous turns
2. `[0]` indexes into that list to get a specific Trace
3. Subsequent segments use `getattr` on Trace objects (for fields like `fn`, `args`, `result`) or key lookup on dicts (for navigating into `args` or `result`)

The resolved value is converted to a string and substituted into the trigger text.

## Full Example: Refund Flow

This example tests a two-turn refund conversation. The first turn searches for orders, and the second turn uses the returned order ID to initiate a refund:

```yaml
agent:
  start: "python -m support_agent.main"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Refund Flow
    tests:
      - turns:
          - trigger: "I want to return the shoes I bought last week"
            expect:
              function_calls:
                - name: search_orders
                  params:
                    category: shoes

          - trigger: "Yes, order &#123;&#123;previous.function_calls[0].result.order_id&#125;&#125;, please refund it"
            expect:
              function_calls:
                - name: initiate_refund
              response:
                contains: "refund"
```

When the second turn executes:

1. Gauntlet looks at all traces collected during the first turn
2. It resolves <code v-pre>{{previous.function_calls[0].result.order_id}}</code> by navigating: traces list -> first trace -> result dict -> order_id key
3. If `search_orders` returned `{"order_id": "ORD-555", ...}`, the trigger becomes: `"Yes, order ORD-555, please refund it"`
4. This resolved trigger is sent to the agent

## Trace Accumulation

Traces accumulate across turns. Each turn sees all traces from every previous turn, not just the immediately preceding one. This means:

- Turn 1 sees no previous traces
- Turn 2 sees traces from turn 1
- Turn 3 sees traces from turns 1 and 2

You can reference traces from any previous turn using the appropriate index:

```yaml
tests:
  - turns:
      - trigger: "Search for laptops"
        expect:
          function_calls:
            - name: search_products

      - trigger: "Show me details on the first result"
        expect:
          function_calls:
            - name: get_product_details

      - trigger: "Add it to cart. My ID is &#123;&#123;previous.function_calls[1].result.product_id&#125;&#125;"
        expect:
          function_calls:
            - name: add_to_cart
```

In the third turn, `function_calls[0]` refers to the `search_products` trace from turn 1, and `function_calls[1]` refers to the `get_product_details` trace from turn 2.

## Early Termination

If any turn fails its assertions, the test stops immediately. No further turns are executed. This prevents cascading failures from confusing the test output -- if turn 1 fails to call the expected tool, there is no point sending turn 2 with a template that references that tool's result.

The test result will include errors only from the turn that failed, along with all traces collected up to that point.

## Combining with Response Assertions

Each turn can include both function call and response assertions:

```yaml
turns:
  - trigger: "What's the status of order #123?"
    expect:
      function_calls:
        - name: get_order
          params:
            order_id: "123"
      response:
        contains: "shipped"

  - trigger: "When will it arrive?"
    expect:
      function_calls:
        - name: get_delivery_estimate
      response:
        matches: "\\d+ business days"
```
