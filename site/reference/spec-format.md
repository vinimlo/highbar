# Spec format

Every key highbar accepts, by level. Any other key is an error, except keys that start with `x-`, which are skipped at every level. Times are in milliseconds.

## Top level

| Key | Type | |
|---|---|---|
| `agent` | mapping | Required. How to reach the agent. |
| `suites` | list | Required, not empty. |
| `defaults` | mapping | Optional. `runs`: how many times each test runs (default 1). `min_pass`: how many runs must pass in a test without its own `min_pass`, with the same values as the test's (default: all). `budget`: what each execution may spend in a test without its own [`budget`](#budget). |

## `agent`

| Key | Type | Default | |
|---|---|---|---|
| `endpoint` | URL | required | Where each message is sent. |
| `start` | command | none | Start the agent: highbar runs it, stops it at the end, and passes `HIGHBAR_TRACES_URL`. Without it, highbar attaches to a running agent. |
| `health` | URL | none | Must answer before tests start. Without it, highbar waits for the endpoint's port. |
| `startup_timeout` | ms | 15000 | How long a started agent has to become ready. |
| `settle_time` | ms | 500 | How long after a reply highbar waits before reading the trace file. |
| `timeout` | ms | 60000 | Each reply, and each session hook. |
| `traces` | path | none | The trace file of an attached agent. Required when a test checks tool calls in attach mode. |
| `request` | mapping | <code v-pre>POST {"message": "{{message}}"}</code> | `method`, `headers`, `body`. |
| `response` | dotted path | first of `response`, `message`, `content`, `text`, `reply` | Where the reply text is in the JSON body. |
| `session` | mapping | none | `setup` and `teardown` hooks, each a command or `{python: "module:function"}`. |
| `correlate` | mapping | <code v-pre>{test: "{{test.id}}"}</code> | The context keys that link a trace record to a test. |

`${NAME}` anywhere under `agent` is replaced by the environment variable when the spec is loaded; a missing variable is an error. Placeholders (<code v-pre>{{message}}</code>, <code v-pre>{{test.id}}</code>, <code v-pre>{{test.name}}</code>, <code v-pre>{{session.&lt;path&gt;}}</code>) are allowed in `request`, `correlate` and session commands. See [attach mode and sessions](/guides/attach-mode).

## `suites[]`

| Key | Type | |
|---|---|---|
| `name` | text | Required. |
| `tests` | list | Required, not empty. |

## `tests[]`

| Key | Type | |
|---|---|---|
| `trigger` | text | The message. Either `trigger` or `turns`. |
| `expect` | mapping | What must hold. Required with `trigger`. With `turns`, it holds only a [`budget`](#budget), for the whole conversation. |
| `turns` | list | A conversation: each turn has `trigger`, `expect` and an optional `name`. |
| `name` | text | Shown in the output instead of the trigger. |
| `id` | text | The test's key in a [report](/reference/report), for when the name or the trigger may change. Default: `<suite>/<name>`, where the name falls back to the trigger. |
| `variations` | integer | How many paraphrases of the trigger to run as well. |
| `runs` | integer ≥ 1 | How many times to run each phrasing. |
| `min_pass` | integer ≥ 1, or fraction in (0, 1] | How many runs must pass. Default: `defaults.min_pass`, else all. A fraction is rounded up. |

Each test's key is unique in a spec. Two tests with the same key are a spec error: give one an `id` or a different `name`.

## `expect`

| Key | Type | |
|---|---|---|
| `function_calls` | list | Calls that must happen. `[]`: no call at all. |
| `ordered` | boolean | The calls happened in the listed order. Needs `function_calls`. |
| `exclusive` | boolean | No call beyond the listed ones. Needs `function_calls`. |
| `forbid` | list of tool names | Tools that must not be called. |
| `response` | matchers | Conditions on the reply text. |
| `budget` | mapping | What may be spent. See [`budget`](#budget). |

At least one of `function_calls`, `forbid`, `response` and `budget` is required.

## `budget`

| Key | Type | |
|---|---|---|
| `max_input_tokens` | integer ≥ 0 | The whole input, cached tokens included. |
| `max_cost` | number ≥ 0 | The cost, compared with the sum in each currency. |

At least one is required. A value equal to the limit is within it. Where a budget is written says what it covers:

| Where | Covers |
|---|---|
| `expect` of a turn | That turn. |
| `expect` of a test | Each execution of the test: all its turns, added up. |
| `defaults.budget` | Each execution of a test without a `budget` in its own `expect`. |

```yaml test
- name: a conversation that stays small
  expect:
    budget: {max_input_tokens: 6000, max_cost: 0.05}
  turns:
    - trigger: "What did I order last week?"
      expect:
        budget: {max_input_tokens: 2000}
    - trigger: "Cancel it"
      expect:
        function_calls:
          - name: cancel_order
```

A test's `budget` replaces `defaults.budget` whole: a limit it leaves out is not taken from the default. A limit on a turn does not take the place of the one on the execution, so a conversation with limits only on its turns still gets `defaults.budget`.

| What happened | Result |
|---|---|
| Within every limit. | The budget passes. |
| Over a limit. | `failed`, and it counts against `min_pass` like any other failed run. |
| The agent wrote no `usage`, or not the field the limit needs. | `error`: nothing was verified. |
| Some of the usage is missing, and what is known is already over a limit. | `failed`: more usage can only add to it. |
| Some of the usage is missing, and what is known is within the limit. | `error`. |
| A `turn` with no test context has usage, and the budget holds only without it. | `error`: it may be this test's. |
| A `turn` with usage was recorded after the test's window, and takes it over a limit. | `failed`, like a late tool call. |

`max_cost` has no currency of its own. A sum in any currency above it fails the budget; costs in more than one currency that are all within it end in `error`, because they do not add up to one number. A cost without a `currency` counts as missing. The agent's cost and the price table are in the [trace contract](/reference/trace-contract#usage-tokens-and-cost).

A budget needs the turn's record, so it counts as a check on what the agent recorded: in attach mode it needs `agent.traces`, and a test with one needs a record of its own, like a test that checks tool calls.

## `function_calls[]`

| Key | Type | |
|---|---|---|
| `name` | text, or list of texts | Required. A list accepts any of the names. |
| `params` | mapping | Must be contained in the call's arguments. Values may be matchers. |
| `result` | mapping | Must be contained in what the tool returned. |
| `error` | boolean, or matchers | `false` (default): the call must not have failed. `true`: it must have. A matcher: its error text must meet it. |

## Matchers

Used in `response`, in values of `params` and `result`, and in `error`:

| Matcher | |
|---|---|
| `equals` | Exactly this value. |
| `contains` / `icontains` | Contains the text (`i`: ignoring case); for a list, has the item. |
| `not_contains` / `not_icontains` | Does not. |
| `matches` / `not_matches` | A regular expression is found (Python `re.search`), or is not. **Case-sensitive.** Checked when the spec is loaded. |
| `imatches` / `not_imatches` | The same, ignoring case. `(?i)` at the start of a pattern does the same in `matches`. |

Negative matchers fail on a value with no text form, such as an object or `null`. Templates (<code v-pre>{{previous.*}}</code>) are only allowed in triggers.
