# Spec format

Every key highbar accepts, by level. Any other key is an error, except keys that start with `x-`, which are skipped at every level. Times are in milliseconds.

## Top level

| Key | Type | |
|---|---|---|
| `agent` | mapping | Required. How to reach the agent. |
| `suites` | list | Required, not empty. |
| `defaults` | mapping | Optional. `runs`: how many times each test runs (default 1). `min_pass`: how many runs must pass in a test without its own `min_pass`, with the same values as the test's (default: all). |

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
| `expect` | mapping | What must hold. Required with `trigger`. |
| `turns` | list | A conversation: each turn has `trigger`, `expect` and an optional `name`. |
| `name` | text | Shown in the output instead of the trigger. |
| `variations` | integer | How many paraphrases of the trigger to run as well. |
| `runs` | integer ≥ 1 | How many times to run each phrasing. |
| `min_pass` | integer ≥ 1, or fraction in (0, 1] | How many runs must pass. Default: `defaults.min_pass`, else all. A fraction is rounded up. |

## `expect`

| Key | Type | |
|---|---|---|
| `function_calls` | list | Calls that must happen. `[]`: no call at all. |
| `ordered` | boolean | The calls happened in the listed order. Needs `function_calls`. |
| `exclusive` | boolean | No call beyond the listed ones. Needs `function_calls`. |
| `forbid` | list of tool names | Tools that must not be called. |
| `response` | matchers | Conditions on the reply text. |

At least one of `function_calls`, `forbid` and `response` is required.

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
| `matches` / `not_matches` | A regular expression is found (Python `re.search`), or is not. Checked when the spec is loaded. |

Negative matchers fail on a value with no text form, such as an object or `null`. Templates (<code v-pre>{{previous.*}}</code>) are only allowed in triggers.
