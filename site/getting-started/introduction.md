# Introduction

## The Problem

73% of teams test AI agents by chatting with them manually. 40% of production failures trace back to incorrect tool calls — wrong function, wrong parameters, or unexpected side effects. And yet, the existing evaluation tools focus on testing the LLM itself (prompt quality, model accuracy, token costs) rather than the agent that wraps it.

The result: agents ship to production with no automated coverage on the behavior that actually matters.

## Philosophy

Nobody uses Cypress to test which browser is better — they test that the **application** works. Highbar applies the same philosophy to AI agents: test the **agent**, not the LLM.

Your agent is software. It receives inputs, calls tools, and produces outputs. Those behaviors are observable and deterministic enough to assert against. Highbar gives you the framework to do exactly that.

## What Highbar Does

1. **You define expected behaviors in YAML specs.** Each spec declares which tools should be called, with what parameters, and what the response should contain.

2. **You instrument your agent's tools with a one-line decorator.** The `@hb.tool` decorator captures every tool invocation — function name, arguments, return value, timing — and writes it to a trace file.

3. **Highbar starts your agent, sends triggers, captures traces, and validates assertions.** The framework handles the full lifecycle: subprocess management, health checks, HTTP triggers, trace collection, and rich terminal reporting.

## What It Looks Like

A spec file (`specs/orders.highbar.yaml`):

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
  health: "http://localhost:8000/health"

suites:
  - name: Order Lookup
    tests:
      - trigger: "Where is my order #12345?"
        expect:
          function_calls:
            - name: search_orders
              params:
                order_id: "12345"
          response:
            contains: "shipped"

      - trigger: "Tell me a joke"
        expect:
          function_calls: []
          response:
            not_contains: "order"
```

Running it:

```bash
highbar run specs/orders.highbar.yaml
```

Terminal output:

```
  Highbar v0.1.0
  Running specs/orders.highbar.yaml

  Order Lookup
    ✓ Where is my order #12345?
    ✓ Tell me a joke

  2 passed, 0 failed (1.2s)
```

Two tests, two different concerns. The first validates that the agent calls the right tool with the right parameters and includes "shipped" in the response. The second is a guardrail test — it ensures the agent does **not** call any tools and does **not** leak order information when the user asks something unrelated.

## Next Steps

- [Installation](/getting-started/installation) — install Highbar via pip or uv
- [Quickstart](/getting-started/quickstart) — build and test your first agent in 5 minutes
- [Core Concepts](/getting-started/concepts) — understand specs, traces, and the execution model
