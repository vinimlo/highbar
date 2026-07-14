# Assertions

Gauntlet provides two categories of assertions: **function call assertions** that validate tool invocations, and **response assertions** that validate the agent's text output.

## Function Call Assertions

The `function_calls` field in the `expect` block asserts that specific tools were called with the expected arguments and return values.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | The function name to match against trace records |
| `params` | No | Expected arguments -- uses subset matching |
| `result` | No | Expected return value -- uses subset matching |

### Basic Usage

Assert that a function was called, regardless of its arguments:

```yaml
expect:
  function_calls:
    - name: get_weather
```

### With Parameter Matching

Assert that a function was called with specific arguments:

```yaml
expect:
  function_calls:
    - name: search_products
      params:
        category: "electronics"
        max_price: 500
```

### With Result Matching

Assert that a function returned specific values:

```yaml
expect:
  function_calls:
    - name: get_order
      params:
        order_id: "ORD-123"
      result:
        status: "shipped"
```

### Multiple Function Calls

Assert that multiple tools were called in the same turn:

```yaml
expect:
  function_calls:
    - name: get_order
      params:
        order_id: "ORD-789"
    - name: track_shipment
```

Each expected call is matched independently against the collected traces. Order does not matter -- Gauntlet finds any trace with a matching function name, then checks params and result.

### Matching Behavior

For each expected function call, Gauntlet:

1. Filters all traces to find those where `trace.fn == expected.name`
2. If no trace matches the name, the assertion fails with: `Expected call to 'get_order', but it was never called`
3. For each matching trace, checks whether `params` and `result` pass subset matching
4. If any matching trace satisfies both checks, the assertion passes

### Empty List: The Guardrail Pattern

An empty `function_calls` list asserts that the agent called **no tools at all**:

```yaml
expect:
  function_calls: []
```

If any tool was called, the assertion fails with a message listing the functions that were invoked. See the [Guardrails](./guardrails.md) guide for detailed patterns.

## Response Assertions

The `response` field validates the text content of the agent's HTTP response.

### `contains`

Asserts that the response includes a specific substring (case-sensitive):

```yaml
expect:
  response:
    contains: "Your order has been shipped"
```

### `not_contains`

Asserts that the response does NOT include a specific substring:

```yaml
expect:
  response:
    not_contains: "error"
```

### `matches`

Asserts that the response matches a regular expression pattern. Uses Python's `re.search`, so the pattern can match anywhere in the response:

```yaml
expect:
  response:
    matches: "order #[A-Z]+-\\d+"
```

### Combining Response Assertions

You can use multiple response assertions together:

```yaml
expect:
  response:
    contains: "refund"
    not_contains: "denied"
    matches: "\\$\\d+\\.\\d{2}"
```

All specified assertions must pass for the test to succeed.

## Subset Matching

Both `params` and `result` use **subset matching**. This means you only need to specify the keys you care about -- any additional keys in the actual data are ignored.

### How It Works

If you write:

```yaml
params:
  order_id: "123"
```

And the actual arguments captured in the trace are:

```json
{"order_id": "123", "format": "json", "include_history": true}
```

The assertion **passes**. Only the keys you listed (`order_id`) are checked. The extra keys (`format`, `include_history`) are not considered.

### Nested Subset Matching

Subset matching works recursively for nested objects:

```yaml
result:
  order:
    status: "shipped"
```

This passes if the actual result contains an `order` key with a dict that has `status: "shipped"`, regardless of any other keys in the `order` object or the top-level result.

## Combining Function Calls and Response Assertions

You can use both assertion types in the same `expect` block:

```yaml
expect:
  function_calls:
    - name: search_orders
      params:
        customer_id: "cust-456"
  response:
    contains: "Found 3 orders"
    not_contains: "error"
```

All assertions must pass for the test to succeed. This lets you verify both that the agent called the right tools and that it communicated the results correctly to the user.
