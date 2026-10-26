# Comparison

highbar does one thing: it checks, with deterministic assertions, the tool calls and the reply of an agent you run, from a YAML spec, and gates CI with its exit code. The tools below do more, and some of them overlap with that one thing. This page says where.

Every statement about another tool links to its documentation, read on 2026-10-26. If something here is wrong or out of date, please [open an issue](https://github.com/vinimlo/highbar/issues).

## At a glance

| | highbar | Promptfoo | DeepEval | Langfuse | Inspect |
|---|---|---|---|---|---|
| Tests written in | YAML | YAML config, with Python/JS for custom providers | Python (pytest) or TypeScript (Vitest) | Python/TS SDK code, or evaluators in the UI | Python |
| Deterministic tool-call checks | Built in: calls, arguments, order, nothing extra, forbidden tools, failed calls | Built in: trajectory assertions on tool use, arguments and sequence | `ToolCorrectnessMetric` (names, order, parameters) | Code you write over the recorded `tool_calls` | A custom scorer you write over the messages |
| LLM-graded checks | No | Yes | Yes | Yes | Yes |
| How it reaches the agent | HTTP to a running agent, which writes a trace file | Providers: model APIs, an HTTP endpoint, scripts, agent SDKs; trajectories from OpenTelemetry spans | The agent as a Python function, instrumented with `@observe` | Traces from production; experiments run your `task` function | Runs the model and its tools itself; other agent frameworks through its agent bridge |
| Repetition | `runs` and `min_pass` per test | `--repeat`, and a pass-rate threshold for the whole eval | `-r N`, a threshold per metric | Not found in its docs | `--epochs`, with reducers like `at_least_n` and `pass_at_k` |
| CI gate | Exit codes 0, 1, 2 | Exit code 100 on failure; a GitHub Action that comments on PRs | `deepeval test run` fails the build | `langfuse/experiment-action` | Check the eval log in a script |
| Needs a server or account | No | No | No (a hosted platform is optional) | A Langfuse instance and API keys | No |
| License | MIT | MIT | Apache 2.0 | MIT, except `ee` folders | MIT |

## Where highbar differs

What the others' docs describe, next to what highbar does:

- **Proof per message.** Promptfoo's trajectory assertions end in an error when there is no trace data at all. highbar also asks, per message, whether the agent handled it: when the agent tags its records with the test (and always in attach mode), a test that checks tool calls needs a record of its own, or it is an `error`.
- **Session hooks that run code.** Promptfoo's HTTP provider can send a session id per test. highbar's session hook is a command or a Python function that opens real state, like a conversation in your database, before each test execution and closes it after; its values can be sent in the request and used to match records to the test.
- **Repetition thresholds per test.** `runs: 5, min_pass: 4` on the flaky test, one run for the rest. Promptfoo's pass-rate threshold is for the whole eval, and Inspect's `--epochs` with `at_least_n` applies to every sample of an eval.

Promptfoo's trajectory assertions are the closest thing: they check tool use, arguments and order from traces. If you already use Promptfoo, they may be all you need.

## Promptfoo

An "open-source CLI and library for evaluating and red-teaming LLM apps" ([intro](https://www.promptfoo.dev/docs/intro/)). Tests are a YAML config with typed assertions.

- **Tool calls.** Deterministic assertions check that calls match their schema (`is-valid-openai-tools-call`), score the tool names (`tool-call-f1`), and, from traces, check that a tool was used, its arguments (`partial` or `exact`), the sequence of calls and the step count (`trajectory:*`). Any assertion can be negated with a `not-` prefix; the docs show no trajectory example of that ([deterministic assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/deterministic/)).
- **Reaching the agent.** An HTTP provider calls a running app; trajectories come from OpenTelemetry spans the app sends to a receiver the CLI runs ([HTTP provider](https://www.promptfoo.dev/docs/providers/http/), [tracing](https://www.promptfoo.dev/docs/tracing/)).
- **Repetition and CI.** `--repeat N`; a pass-rate threshold for the whole eval; exit code 100 on failures ([command line](https://www.promptfoo.dev/docs/usage/command-line/)); a GitHub Action that compares before and after on a PR ([GitHub Action](https://www.promptfoo.dev/docs/integrations/github-action/)).
- **Stronger than highbar at:** red teaming, a web viewer for comparing outputs, LLM-graded assertions next to deterministic ones, argument matching with an exact mode, defaults and ignored fields, and many providers, including agent SDKs.

## DeepEval

An open-source LLM evaluation framework that runs inside pytest or Vitest ([getting started](https://deepeval.com/docs/getting-started)).

- **Tool calls.** `ToolCorrectnessMetric` compares the tools called with the expected ones, optionally by order, exact match and parameters, and can add an LLM step that judges whether the choice was optimal ([tool correctness](https://deepeval.com/docs/metrics-tool-correctness)). No option to forbid a tool was found in its docs.
- **Reaching the agent.** The agent runs in the test process as a Python function instrumented with `@observe`, or the test fills in the tools called itself ([agent quickstart](https://deepeval.com/docs/getting-started-agents)).
- **Repetition and CI.** `deepeval test run -r N`, a threshold per metric, and failing metrics fail the build ([CI/CD](https://deepeval.com/docs/evaluation-unit-testing-in-ci-cd)).
- **Stronger than highbar at:** LLM-judged agent metrics (task completion, plan quality, step efficiency, argument correctness), evaluation of individual spans, and pytest integration.

## Langfuse

"An open-source AI engineering platform" for tracing, prompts and evaluation, self-hosted or in Langfuse Cloud ([docs](https://langfuse.com/docs)).

- **Tool calls.** Code evaluators can read the recorded tool calls as a structured field, and LLM-as-a-judge evaluators can be given them ([code evaluators](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators), [July 2026 update](https://langfuse.com/blog/2026-07-31-langfuse-july-update)). The checks are code you write or an LLM judge; no built-in "must call / must not call" check was found.
- **Reaching the agent.** Traces sent by your instrumented app, and experiments that run your `task` function from the SDK ([experiments via SDK](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)).
- **CI.** `langfuse/experiment-action` fails the job on a regression your script detects, and needs a Langfuse instance and API keys ([CI/CD](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd)).
- **Stronger than highbar at:** everything around production: tracing, dashboards for quality, cost and latency, prompt management, datasets, annotation, and evaluators that run on live traffic.

## Inspect

"A framework for frontier AI evaluations", from the UK AI Security Institute ([Inspect](https://inspect.aisi.org.uk/)).

- **Tool calls.** The built-in scorers are text-based or model-graded; a custom scorer can read the tool calls in the conversation it receives ([scorers](https://inspect.aisi.org.uk/scorers.html)).
- **Reaching the agent.** Inspect calls the model and runs the tools itself; agents built with other frameworks run through its agent bridge ([agents](https://inspect.aisi.org.uk/agents.html), [agent bridge](https://inspect.aisi.org.uk/agent-bridge.html)).
- **Repetition.** `--epochs` with reducers such as `at_least_n` and `pass_at_k` ([options](https://inspect.aisi.org.uk/options.html)).
- **Stronger than highbar at:** sandboxed evaluations (Docker and others, one sandbox per sample), built-in agents and tools, and a log viewer ([sandboxing](https://inspect.aisi.org.uk/sandboxing.html)).

## When to use which

- You want a regression suite for what your agent does, written as specs, run against the agent you deploy, with no LLM in the verdict: **highbar**.
- You compare prompts and models, red-team an app, or want LLM-graded quality next to deterministic checks: **Promptfoo**.
- Your agent is Python code you can call in a test, and you want graded agent metrics in pytest: **DeepEval**.
- You need observability, datasets and evaluation on production traffic: **Langfuse**. highbar and Langfuse do not compete for the same job.
- You evaluate model or agent capabilities in sandboxes: **Inspect**.
