# Assertions

Highbar provides two categories of assertions: **function call assertions** that validate tool invocations, and **response assertions** that validate the agent's text output.

## Function Call Assertions

The `function_calls` field in the `expect` block asserts that specific tools were called with the expected arguments and return values.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | The function name to match against trace records, or a list of names of which any one satisfies the expectation |
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

Each expected call is matched independently against the collected traces. Order does not matter -- Highbar finds any trace with a matching function name, then checks params and result.

### Alternative Tools

Some intents have more than one correct answer. Give `name` a list and any one of the listed tools satisfies the expectation:

```yaml
expect:
  function_calls:
    - name: [open_catalog, list_categories]
```

`params` and `result` are shared by every alternative: the expectation passes when any listed tool was called with a matching payload. "Tool A with X, or tool B with Y" cannot be expressed -- only list tools together when the same `params`/`result` (or none) apply to all of them.

When none of them was called, the failure reads: `Expected call to one of ('open_catalog' or 'list_categories'), but the agent called: search_products`.

### Matching Behavior

For each expected function call, Highbar:

1. Filters all traces to find those where `trace.fn` is `expected.name` (or, when `name` is a list, any of the listed names)
2. If no trace matches, the assertion fails naming what was expected and what the agent actually called: `Expected call to 'get_order', but the agent called: track_shipment` (or `called: nothing` when no tool ran)
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

A nested dict is subset-matched too, at every level. To require an exact nested dict, use the `equals` matcher described below.

## Matchers

A plain expected value compares by equality. When the value is written by the model -- and its case or wording is not something the agent should be failed for -- use a **matcher** instead: a dict whose keys are all matcher names.

```yaml
expect:
  function_calls:
    - name: search_products
      params:
        product_name: {icontains: "coca"}
        order_id: {matches: "^[0-9]+$"}
      result:
        order:
          status: {contains: "ship"}       # matchers work at any depth
```

| Matcher | Passes when |
|---------|-------------|
| `equals` | The actual value equals the operand exactly. The operand is always a literal |
| `contains` | The operand is a substring of the actual text (**case-sensitive**, same as `response: contains`), or an item of the actual list |
| `icontains` | Same as `contains`, ignoring case |
| `not_contains` | The actual value was checked and does not contain the operand (case-sensitive) |
| `not_icontains` | Same as `not_contains`, ignoring case |
| `matches` | The operand, a regular expression, is found in the actual text (`re.search`) |

Several matchers in one dict must all hold: `{icontains: "coca", not_icontains: "zero"}`.

### What a matcher can be applied to

- **Text and numbers.** Numbers are compared through their text form, so `{contains: "123"}` and `{matches: "123"}` both pass on `12345`.
- **Lists**, for the `contains` family: membership instead of substring.
- **`null` and nested objects are never matched by a text matcher.** `matches`, `contains` and `icontains` fail on them -- and so do `not_contains` and `not_icontains`. A negative assertion that could not be evaluated fails rather than passing without having checked anything: `error: {not_contains: "timeout"}` is red when `error` is `null` or an object.

### Literal dicts that look like a matcher

::: warning Behaviour change
Any dict whose keys are **all** matcher names is read as a matcher. If a tool takes an argument that is literally shaped like one (a Prisma- or Notion-style filter such as `{contains: "shoes"}`), an existing spec that compared it by equality changes meaning. Wrap the literal in `equals`:

```yaml
params:
  filter: {equals: {contains: "shoes"}}
```
:::

### Validation

`highbar validate` (and `highbar run`, before starting the agent) rejects:

- an invalid regular expression in `matches`;
- a dict that mixes matcher names with other keys, such as `{contains: "coca", size: "L"}`;
- a near miss of a matcher name -- `contain`, `match`, `Contains`, `not-contains` -- which would otherwise be silently compared as a literal dict. If the key really is literal, wrap the dict in `equals`.

Matchers inside a list (`items: [{name: {contains: "x"}}]`) are not supported; a list compares by equality.

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
