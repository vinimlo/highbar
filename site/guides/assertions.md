# Assertions

Everything a test checks goes under `expect`. A test needs at least one check, and every check must hold for the test to pass.

## Tool calls: `function_calls`

Each entry describes a call the agent must make:

```yaml test
trigger: "Compare the two cheapest laptops"
expect:
  function_calls:
    - name: search_products
      params: {category: "laptop"}
    - name: search_products
    - name: compare
```

- **Each entry needs a call of its own.** Two `search_products` entries need two calls. highbar pairs entries with calls so that a call that fits a narrow entry is not spent on a broad one first.
- **`name`** is a tool name, or a list of names when any of them will do: `name: [search, search_products]`.
- **`params`** must be contained in the call's arguments: keys you leave out are not checked, and nested objects are compared the same way.
- **`result`** works like `params`, on what the tool returned.
- **Other calls are allowed** unless the test says `exclusive: true`.

A value in `params` or `result` is compared as is, or with a matcher:

```yaml test
trigger: "Find me a red dress under 200"
expect:
  function_calls:
    - name: search_products
      params:
        query: {icontains: "dress"}
        color: "red"
        max_price: {matches: "^(1[0-9]{2}|200)$"}
```

| Matcher | Holds when the value |
|---|---|
| `equals` | is exactly the operand. Use it to compare a whole object, or an object that looks like a matcher. |
| `contains`, `icontains` | contains the text (`icontains` ignores case). For a list: has the item. |
| `not_contains`, `not_icontains` | does not contain it. |
| `matches`, `not_matches` | matches the regular expression somewhere (Python's `re.search`), or does not. |

A value that has no text form (an object, a list) never satisfies a negative matcher: "it does not contain the card number" is not evidence when there is no text to look at.

### A call that failed

A tool can raise, or return an error. The SDK records that in the call's `error` field, and a call with an `error` does not satisfy an entry, because calling a tool is not the same as getting what it does. When the failure is the behavior under test, say so:

```yaml test
trigger: "Where is order 1042?"
expect:
  function_calls:
    - name: get_order
      error: true
  response:
    icontains: "try again"
```

`error` takes `true`, `false` (the default), or a matcher the error text must meet: `error: {contains: "Timeout"}`.

### Order and nothing else: `ordered`, `exclusive`

```yaml test
trigger: "Cancel order 1042"
expect:
  ordered: true
  exclusive: true
  function_calls:
    - name: get_order
    - name: cancel_order
```

- **`ordered: true`**: the calls happened in this order, by when they ran. Each must start after the one before it ended, so two calls the agent fires at the same time are not "in order". Other calls may come in between unless the test is also exclusive.
- **`exclusive: true`**: no call beyond the expected ones.

### Calls that must not happen: `forbid`

```yaml test
trigger: "Delete my account right now"
expect:
  forbid: [delete_account, close_account]
```

A forbidden tool fails the test even when the call itself failed: trying is the defect. `function_calls: []` is the stricter form, no tool at all. See [guardrails](/guides/guardrails).

## The reply: `response`

`response` takes the same matchers, applied to the agent's reply text:

```yaml test
trigger: "Where is order 1042?"
expect:
  response:
    contains: "1042"
    not_icontains: "sorry"
    not_matches: '\b4[0-9]{3}([ -]?[0-9]{4}){3}\b'
```

Each condition that fails gets its own line in the output, ending with what the agent said, cut at 300 characters.

## What a failure says

The message gives the most basic reason first:

| The agent | highbar says |
|---|---|
| never called the tool | `Expected call to 'get_order', but the agent called: search` |
| called it, and every call failed | `'get_order' was called but failed: TimeoutError: upstream timed out` |
| called it fewer times than the entries need | `expected 2 calls to 'search', got 1 that fit; 1 more failed: TimeoutError: slow` |
| called it with other arguments | the `params` or `result` that differ, with what it got |
| called them in another order | `calls matched but out of order: expected get_order, cancel_order; got cancel_order, get_order` |
| made a call nobody expected (`exclusive`) | `unexpected calls: delete_account` |
| called a forbidden tool | `forbidden tool 'delete_account' was called` |
