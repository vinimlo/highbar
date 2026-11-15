# Spec format

Every key highbar accepts, by level. Any other key is an error, except keys that start with `x-`, which are skipped at every level. Times are in milliseconds.

## Top level

| Key | Type | |
|---|---|---|
| `agent` | mapping | Required. How to reach the agent. |
| `suites` | list | Required, not empty. |
| `defaults` | mapping | Optional. `runs`: how many times each test runs (default 1). `min_pass`: how many runs must pass in a test without its own `min_pass`, with the same values as the test's (default: all). `budget`: what each execution may spend in a test without its own [`budget`](#budget). `session`: `{args: {...}}` for every test; a suite's and a test's own win, key by key. |

## `agent`

| Key | Type | Default | |
|---|---|---|---|
| `endpoint` | URL | required | Where each message is sent. |
| `start` | command | none | Start the agent: highbar runs it, stops it at the end, and passes `HIGHBAR_TRACES_URL`. Without it, highbar attaches to a running agent. A `${VAR}` in it is shown in the output: pass a token in the environment, which the agent inherits. |
| `health` | URL | none | Must answer before tests start. Without it, highbar waits for the endpoint's port. |
| `startup_timeout` | ms | 15000 | How long a started agent has to become ready. |
| `settle_time` | ms | 500 | How long after a reply highbar waits before reading the trace file. |
| `timeout` | ms | 60000 | Each reply, and each session hook. |
| `traces` | path or glob | none | The trace file of an attached agent, or a glob of files, one per process (`var/traces/*.jsonl`), read together in time order. A path that names a file is that file, even with a `[` in it. See [several processes](/guides/attach-mode#an-agent-with-several-processes). Required when a test checks tool calls in attach mode. |
| `request` | mapping | <code v-pre>POST {"message": "{{message}}"}</code> | `method`, `headers`, `body`. |
| `response` | dotted path | first of `response`, `message`, `content`, `text`, `reply` | Where the reply text is in the JSON body. |
| `session` | mapping | none | `setup` and `teardown` hooks, each a command, `{python: "module:function"}` or `{http: {url, method, headers, body}}`. |
| `run` | mapping | none | `setup` and `teardown` hooks that run once per spec: the `setup` before the agent starts, the `teardown` on every way out after it. The same three kinds as `session`, with no placeholders. See [before and after the run](/guides/attach-mode#before-and-after-the-run). |
| `correlate` | mapping | <code v-pre>{test: "{{test.id}}"}</code> | The context keys that link a trace record to a test. |

`${NAME}` anywhere under `agent` is replaced by the environment variable when the spec is loaded; a missing variable is an error. A value in `request`, `session`, `run` or a URL's password, query or fragment is masked in the output; the `traces` path, `start` and a URL's host and path are shown ([which values are masked](/guides/attach-mode)). Placeholders (<code v-pre>{{message}}</code>, <code v-pre>{{test.id}}</code>, <code v-pre>{{test.name}}</code>, <code v-pre>{{test.args.&lt;path&gt;}}</code>, <code v-pre>{{session.&lt;path&gt;}}</code>) are allowed in `request`, `correlate`, session commands and the URL, headers and body of a session's HTTP hook; `run` hooks take none. See [attach mode and sessions](/guides/attach-mode).

## `suites[]`

| Key | Type | |
|---|---|---|
| `name` | text | Required. |
| `tests` | list | Required, not empty. |
| `session` | mapping | `{args: {...}}` for every test of the suite. A test's own args win, key by key. |

## `tests[]`

| Key | Type | |
|---|---|---|
| `trigger` | text | The message. Either `trigger` or `turns`. It can name a value of the session, <code v-pre>{{session.&lt;path&gt;}}</code>, when the agent has a `session`, in a test without `variations`. |
| `expect` | mapping | What must hold. Required with `trigger`. With `turns`, it holds only a [`budget`](#budget), for the whole conversation. |
| `turns` | list | A conversation: each turn has `trigger`, `expect` and an optional `name`. The last turn can have `variations`: paraphrases of its trigger, the turns before it as written. See [variations on the last turn](/guides/multi-turn#variations-on-the-last-turn). |
| `name` | text | Shown in the output instead of the trigger. |
| `id` | text | The test's key in a [report](/reference/report), for when the name or the trigger may change. Default: `<suite>/<name>`, where the name falls back to the trigger. |
| `variations` | integer | How many paraphrases of the trigger to run as well. On a multi-turn test it goes on the last turn instead. |
| `runs` | integer ≥ 1 | How many times to run each phrasing. |
| `min_pass` | integer ≥ 1, or fraction in (0, 1] | How many runs must pass. Default: `defaults.min_pass`, else all. A fraction is rounded up. |
| `session` | mapping | `{args: {...}}`: a JSON object for this test's session hooks and <code v-pre>{{test.args.&lt;path&gt;}}</code>. See [a state of its own](/guides/attach-mode#a-state-of-its-own). |
| `history` | list | The conversation before the test's first message, each `{role, content}` with `role` user or assistant, for the session's setup to seed. Needs `agent.session` with a `setup`: a Python one, or a command or HTTP one that uses <code v-pre>{{test.history}}</code>. In a single-turn test, <code v-pre>{{test.history}}</code> in `agent.request` hands it on too, for an agent with no state of its own. See [starting in the middle of a conversation](/guides/attach-mode#starting-in-the-middle-of-a-conversation). |
| `expect_status` | `failed` or `error` | The state this test must end in: a negative control. It passes when it ends exactly there, and fails when it passes or ends in the other state. See [a test that must fail](/guides/assertions#a-test-that-must-fail-expect-status). |

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
| `check` | mapping | `{python: "module:function"}`: a check of your own, which returns the failures it found. See [a check of your own](/guides/assertions#a-check-of-your-own-check). |

At least one of `function_calls`, `forbid`, `response`, `budget` and `check` is required.

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

A limit on a turn covers the records read in that turn's window. Records carry no turn id, so a `turn` recorded late lands in the next turn's window and counts against that turn's limit, and one read after the test's last window counts against the last turn's. The limit on the execution counts it either way.

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
| `lt` / `lte` / `gt` / `gte` | A number below, at most, above, or at least the operand, which must be a finite number. Only in `params` and `result`. |
| `len` / `len_lte` | A list of exactly, or at most, this many items (a whole number, 0 or more). Only in `params` and `result`. |
| `in` | Equal to one of the listed values (a list with at least one). Only in `params` and `result`. |
| `contains_all` | Contains each listed item, as `contains` does (a list with at least one). Only in `params` and `result`. |
| `any` / `all` | A list where some item, or every item, matches the operand, which is any expected value. `all` fails on an empty list. Only in `params` and `result`. |

Negative matchers fail on a value with no text form, such as an object or `null`. An empty text operand is refused; `equals` takes one. An empty object as a value checks that the key is there and holds an object. A bound fails on a value that is not a number (a numeric string and a boolean included), and a length on a value that is not a list. Templates (<code v-pre>{{previous.*}}</code>) are only allowed in triggers.
