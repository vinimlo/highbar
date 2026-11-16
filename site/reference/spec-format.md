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
| `traces.path`, `traces.format` | mapping | `format: highbar` | `traces` written as `{path, format}`. `path` is what `traces` is as a path. `format` is `highbar`, the [trace contract](/reference/trace-contract), or `otlp`, OpenTelemetry spans as the Collector's `file` exporter writes them: each message then carries a W3C `traceparent`, and a span belongs to the test whose trace id it carries, so `correlate` cannot be set. See [OpenTelemetry traces](/guides/opentelemetry). |
| `request` | mapping | <code v-pre>POST {"message": "{{message}}"}</code> | `method`, `headers`, `body`. |
| `response` | dotted path | first of `response`, `message`, `content`, `text`, `reply` | Where the reply text is in the JSON body. |
| `reply` | `body` or `turn` | `body` | Where the reply comes from: the HTTP body, or the `response` of the test's `turn` record, for an agent that answers later through a queue. See [an agent that answers later](/guides/connecting-your-agent#an-agent-that-answers-later). |
| `session` | mapping | none | `setup` and `teardown` hooks, each a command, `{python: "module:function"}` or `{http: {url, method, headers, body}}`. |
| `run` | mapping | none | `setup` and `teardown` hooks that run once per spec: the `setup` before the agent starts, the `teardown` on every way out after it. The same three kinds as `session`, with no test's placeholder; what the `setup` returns is <code v-pre>{{run.&lt;path&gt;}}</code>. See [before and after the run](/guides/attach-mode#before-and-after-the-run). |
| `correlate` | mapping | <code v-pre>{test: "{{test.id}}"}</code> | The context keys that link a trace record to a test. |
| `llm_proxy` | boolean or mapping | `false` | Count the agent's model calls at the source: highbar puts a proxy between the agent and its provider, and what the proxy reads is the usage budgets and `--max-cost` hold. A mapping, `{openai: <url>, anthropic: <url>}`, also says where each provider's calls go. Only with `start`. See [usage counted at the source](#usage-counted-at-the-source-agent-llm-proxy). |

`${NAME}` anywhere under `agent` is replaced by the environment variable when the spec is loaded; a missing variable is an error. A value in `request`, `session`, `run` or a URL's password, query or fragment is masked in the output; the `traces` path, `start` and a URL's host and path are shown ([which values are masked](/guides/attach-mode)). Placeholders (<code v-pre>{{message}}</code>, <code v-pre>{{test.id}}</code>, <code v-pre>{{test.name}}</code>, <code v-pre>{{test.args.&lt;path&gt;}}</code>, <code v-pre>{{session.&lt;path&gt;}}</code>, <code v-pre>{{run.&lt;path&gt;}}</code>) are allowed in `request`, `correlate`, session commands and the URL, headers and body of a session's HTTP hook; the `run` teardown takes only <code v-pre>{{run.&lt;path&gt;}}</code>, and the `run` setup none. See [attach mode and sessions](/guides/attach-mode).

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
| `llm` | mapping | The test's calls to the model, from its `llm` records: `model`, `fallback`, `last_role`, `max_calls`. See [calls to the model](/guides/assertions#calls-to-the-model-llm). |
| `retrieval` | mapping | What entered the prompt, from the test's `retrieval` records: `includes`, `excludes`, `max_items`, `max_tokens`, `chunks_in_order`. See [what entered the prompt](/guides/assertions#what-entered-the-prompt-retrieval). |

At least one of `function_calls`, `forbid`, `response`, `budget`, `check`, `llm` and `retrieval` is required.

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

### Usage counted at the source: `agent.llm_proxy`

A budget and `--max-cost` hold what the agent writes in its `turn` records. An agent that leaves a call out, or reports `cost: 0` for a key it brings itself, passes a budget it went over. With `llm_proxy: true`, highbar counts the calls itself:

```yaml agent
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:8000/chat"
  llm_proxy: true
```

highbar starts a proxy on a free local port before the agent, and starts the agent with `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` pointing at it. The official SDKs, and most clients built on them, read those variables. Each request goes on to where the agent would have sent it (the variable's value in highbar's environment before it set it, or the provider's own URL), with its key untouched, and the reply comes back as the provider sent it.

An agent that keeps its base URL in a `.env` file of its own, read when it starts, has none in highbar's environment, so its calls would go to the provider's own URL. Give the upstream in the spec instead, for one provider or both:

```yaml agent
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:8000/chat"
  llm_proxy:
    openai: "https://openrouter.ai/api/v1"
```

An agent that loads its `.env` with `override=True` replaces highbar's `OPENAI_BASE_URL` with its own, and its calls go around the proxy: the warning that a call went around it, or that none went through, says so. On the way, the proxy reads the usage of each model call: OpenAI-compatible `chat/completions` and `responses` (OpenRouter, DeepSeek and local servers included) and Anthropic `messages`, streamed or not. It writes one `llm` record per call to the trace file ([the proxy's records](/reference/trace-contract#records-from-highbar-s-llm-proxy)). It never logs a header or a body.

| What happened | Result |
|---|---|
| The proxy read the turn's model calls. | Their tokens are the turn's usage, and their cost: the provider's own (OpenRouter's `usage.cost`), or the price table's on the model the reply names. The agent's `usage` is shown in the report as `agent_usage`, and not added. |
| The agent's `cost` is more than the proxy's. | The larger holds. A `cost: 0`, or any cost below the proxy's, never lowers it. |
| The proxy cannot price a call (a deployment name, a model of your own, one newer than the table). | The agent's `cost` is a lower bound: over `max_cost` or `--max-cost` the budget fails and the run stops, as without the proxy. Under it, the cost is incomplete, and a `max_cost` is an `error`. |
| The provider rejected a call with a 4xx and no usage, like a 429 the SDK then retried. | It used nothing: zero tokens, no cost, and its `error` in the record. A 5xx may have run the model, so it is a call the proxy could not read. |
| The agent's `turn` reports more tokens than the proxy saw. | A call went around the proxy. A warning names the test, and the larger count holds. |
| A model call whose usage the proxy could not read: a stream that ended without it, a 5xx, a reply cut off, a provider it could not reach. | `error` for the test's budget and for `--max-cost`, never zero tokens. |
| A call that is not a model call, like a list of the models. | Passed through, and not read: a warning says so, and it counts for no budget. |
| A turn with no model call through the proxy. | Its own `usage` counts, as without the proxy. |
| The proxy could not write a record to the trace file. | Every test of the run with a budget, or with `--max-cost`, is an `error`. |

A model name with a snapshot date that the price table does not list, like `gpt-4.1-2025-04-14` or `claude-sonnet-4-5-20250929`, is priced as the model without its date.

Tests run one at a time with it: the agent's calls carry no test id, so a call belongs to the test in flight when the proxy saw it, and `--jobs` above 1 exits with code 2. `expect.llm` still checks the agent's own `llm` records, not the proxy's. In attach mode the agent's environment is not highbar's to set, and the agent may serve others, so `llm_proxy` there is an error in the spec.

## `function_calls[]`

| Key | Type | |
|---|---|---|
| `name` | text, or list of texts | Required. A list accepts any of the names. |
| `params` | mapping | Must be contained in the call's arguments. Values may be matchers. |
| `result` | mapping | Must be contained in what the tool returned. |
| `returns` | any expected value | The whole return value: a matcher, a list, a text or a number, for a tool that returns something other than an object. `null` is refused; write `{equals: null}`. |
| `error` | boolean, or matchers | `false` (default): the call must not have failed. `true`: it must have. A matcher: its error text must meet it. |

## Matchers

Used in `response`, in values of `params` and `result`, and in `error`:

| Matcher | |
|---|---|
| `equals` | Exactly this value. A boolean is never a number, here, in `in`, `contains` and a plain value. |
| `contains` / `icontains` | Contains the text (`i`: ignoring case); for a list, has the item. |
| `not_contains` / `not_icontains` | Does not. |
| `matches` / `not_matches` | A regular expression is found (Python `re.search`), or is not. **Case-sensitive.** Checked when the spec is loaded. |
| `imatches` / `not_imatches` | The same, ignoring case. `(?i)` at the start of a pattern does the same in `matches`. |
| `lt` / `lte` / `gt` / `gte` | A number below, at most, above, or at least the operand, which must be a finite number. Only in `params` and `result`. |
| `len` / `len_lte` | A list of exactly, or at most, this many items (a whole number, 0 or more). Only in `params` and `result`. |
| `in` | Equal to one of the listed values (a list with at least one). Only in `params` and `result`. |
| `contains_all` | Contains each listed item, as `contains` does (a list with at least one). Only in `params` and `result`. |
| `any` / `all` | A list where some item, or every item, matches the operand, which is any expected value. `all` fails on an empty list. Only in `params` and `result`. |

Negative matchers fail on a value with no text form, such as an object, `null` or a boolean, and every text matcher fails on a boolean or with one as its operand. An empty text operand is refused; `equals` takes one. An empty object as a value checks that the key is there and holds an object. A bound fails on a value that is not a number (a numeric string and a boolean included), and a length on a value that is not a list. Templates (<code v-pre>{{previous.*}}</code>) are only allowed in triggers.
