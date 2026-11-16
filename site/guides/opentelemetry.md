# OpenTelemetry traces

An agent already instrumented with OpenTelemetry does not need highbar's SDK: highbar can read the spans it exports. The OpenTelemetry Collector writes them to a file with its `file` exporter, and highbar reads that file as it reads its own [trace contract](/reference/trace-contract), from where it stopped last time.

This works in [attach mode](/guides/attach-mode): the agent and the Collector are already running, and highbar sends messages and reads the file.

```yaml agent
agent:
  endpoint: "http://localhost:8000/chat"
  traces:
    path: var/otel/traces.jsonl
    format: otlp
  settle_time: 1500
```

`traces.format` is `highbar` unless it says `otlp`, and `traces: <path>` is the same as `traces: {path: <path>}`.

## The Collector

The agent exports its spans to the Collector over OTLP, and the Collector's `file` exporter writes each export as one line of OTLP JSON:

```yaml collector
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch:
    timeout: 100ms
exporters:
  file:
    path: /var/otel/traces.jsonl
    flush_interval: 100ms
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [file]
```

Mount `/var/otel` where highbar reads `var/otel`. Leave the exporter's `format` at its default, `json`: highbar does not read `proto`. Do not turn on rotation for a run.

## How a span finds its test

Each message highbar sends carries a W3C `traceparent` header next to `X-Highbar-Test`, with a new trace id for each test execution. The trace id is the test's: a span that carries it belongs to that test, and a span of any other trace does not.

So the agent's instrumentation must continue the incoming trace: an HTTP server instrumentation (FastAPI, Flask, Express, ASP.NET...) does, with the default `tracecontext` propagator. A test that checks tool calls or a budget and finds no span of its trace is an `error` ("not observed"), never a pass. That is also what you see when the server starts a new trace for each request.

`agent.correlate` cannot be used with `format: otlp`: the trace id is the correlation.

## What highbar reads

A tool call is a span that the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) (in Development status, so they may still change) call `execute_tool`, or an OpenInference `TOOL` span. A turn, with its reply text and its tokens, is an `invoke_agent` span or an OpenInference `AGENT` span. The full mapping is in [the trace contract](/reference/trace-contract#opentelemetry-spans-otlp). Other spans (`chat`, `LLM`, `RETRIEVER`...) are not read yet.

A span with status `ERROR`, or with an `error.type`, is a failed call: it does not satisfy an expected call unless the call declares `error`, and a `forbid` fails on it as on any call.

## Turn on content capture

The GenAI conventions leave a tool's arguments and result out of the span unless content capture is turned on. Without the arguments, highbar cannot check `params`: the test is an `error` that says so, never a pass on empty arguments. An expected call that names only the tool still passes.

Turn it on in the agent's environment while it runs under highbar:

```bash
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
```

Some SDKs and frameworks have their own switch for it; use theirs.

## Export the spans before highbar reads

highbar reads the file `settle_time` after each reply. The default `BatchSpanProcessor` exports every 5 seconds, so for evals:

- use a `SimpleSpanProcessor`, which exports each span when it ends, or set `OTEL_BSP_SCHEDULE_DELAY=100` (milliseconds);
- keep the Collector's batch timeout and the file exporter's `flush_interval` short, as above;
- set `settle_time` to more than the whole path takes, from the agent's export to the line in the file.

A span that arrives after its test was read counts for that test, as any late record does: the test is judged again, and a forbidden call that arrives late fails it. After the last test highbar waits `settle_time` once more and reads once more; a span that arrives after that is never read.

## What v0 does not do

- `format: otlp` is for attach mode only.
- An OTLP file declares no tools, so a guardrail (`forbid`, or `function_calls: []`) passes only because no span of that tool was read: it cannot tell a tool with no span from a tool that was not called. A `forbid` on a tool never seen in the run ends with a warning that says so. Make sure every tool emits an `execute_tool` or `TOOL` span.
- `chat`/`LLM` and `RETRIEVER` spans, and so `expect.llm` and `expect.retrieval`, are left for a later version.
