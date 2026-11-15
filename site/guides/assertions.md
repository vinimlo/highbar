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
| `matches`, `not_matches` | matches the regular expression somewhere (Python's `re.search`), or does not. **Case-sensitive:** `matches: "eleven years"` fails on "Eleven years" at the start of a sentence. |
| `imatches`, `not_imatches` | the same, ignoring case. `(?i)` at the start of a pattern does the same in `matches`. |
| `lt`, `lte`, `gt`, `gte` | is a number below, at most, above, or at least the operand. |
| `len`, `len_lte` | is a list of exactly, or at most, that many items. |
| `in` | equals one of the listed values: `plan: {in: [basic, pro]}`. |
| `contains_all` | contains each listed item, as `contains` does: every piece of text, or every item of a list. |
| `any`, `all` | is a list where some item, or every item, matches the operand. The operand is any expected value: an object compared as `params` is, or a matcher. |

A value that has no text form (an object, a list) never satisfies a negative matcher: "it does not contain the card number" is not evidence when there is no text to look at.

An empty text in a matcher (`contains: ""`, `matches: ""`) is refused when the spec loads: `""` is in every text, so it checks nothing. To compare with an empty text, write `{equals: ""}`. An empty object is a check: `options: {}` says that `options` is there and is an object, whatever it holds; `{equals: {}}` says it is an empty one.

Bounds and lengths hold only on what they measure. A bound fails on anything that is not a number, a numeric string (`"3"`) and `true` included, and a length fails on anything that is not a list, a string included: a value of the wrong type is a defect the bound is there to catch. Two conditions make a range:

```yaml test
trigger: "Show me the three newest documents about refunds"
expect:
  function_calls:
    - name: search_documents
      params:
        limit: {gte: 1, lte: 3}
      result:
        documents: {len_lte: 3}
```

`any` and `all` reach into the items of a list, so a test can say what one of the documents must be, or what each of them must be:

```yaml test
trigger: "What is the refund policy?"
expect:
  function_calls:
    - name: retrieve
      result:
        documents:
          len_lte: 3
          any: {id: "policy-7", chunks: [0, 1, 2]}
          all: {score: {gte: 0.5}}
```

`all` fails on an empty list: every item of nothing matched, and nothing was checked. When an empty list is fine and its items are text or numbers, say what none of them may be with `not_contains`, which holds on `[]`: `tags: {not_contains: "private"}`. `in` and `contains_all` need a list with at least one value, and `any: {}`, `all: {}` and an empty text in `contains_all` are refused when the spec loads, because they hold on anything.

These matchers apply to values of `params` and `result`; `response` and `error` refuse them. A reply and an error are text, where a bound or a length could never hold. A literal object whose keys are all matcher names is read as a matcher: to compare `{lt: 5}` itself, write `{equals: {lt: 5}}`.

### A call that failed

A call with an `error` does not satisfy an entry, because calling a tool is not the same as getting what it does. `@hb.tool` records an `error` when the tool raises. A tool that returns its failure as data (`{"error": "upstream timed out"}`) has returned a result, and the call counts: raise instead, or record the call with `hb.record(..., error=...)`. When the failure is the behavior under test, say so:

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

## A test that must fail: `expect_status`

A negative control proves the plumbing: a tool that fails must fail the test, and a test highbar cannot verify, like a budget on usage the agent does not report, must end in `error`. Without a way to say so, the run is red when the control works, and it gets checked by hand. `expect_status` says which state the test must end in:

```yaml test
name: "a broken get_order fails the test"
trigger: "Where is order 1042?"
expect:
  function_calls:
    - name: get_order
expect_status: failed
```

- The test passes when it ends exactly in that state, `failed` or `error`. The terminal marks it `✓ a broken get_order fails the test (expected failed)`, on one line.
- It fails when it passes, with `passed, but expect_status is failed: this negative control no longer catches what it was written for`. It also fails when it ends in the other state (`ended error, but expect_status is failed`): a control that expects a failure and could not verify anything proves nothing.
- The state is the test's, after `runs` and `min_pass`, and after late calls: a late call that fails a control makes it pass, and the run says so in a warning.
- `error` is any state highbar could not verify, an agent that timed out included.
- A run that cannot observe the agent at all stops before its first message, with exit code 2, and no control is judged. That is a managed agent (`agent.start`) whose SDK never writes its `hello`, or an attached agent whose `agent.traces` file does not exist. The check runs only when a test of the spec needs a record. An attached agent whose trace file exists but stays empty does not stop the run: each test that needs a record ends in `error`, so a control expecting `error` passes.
- In the [report](/reference/report#each-test), `status` is the judged one and `expect_status` says what was expected. The phrasings and executions keep what happened, and so do `errors`.

