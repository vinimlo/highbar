# Introduction

highbar runs evals for AI agents that behave like tests. You state what the agent must do for a message, highbar sends the message to the agent you ship, and it checks what the agent did: the tools it called, in which order, with which arguments, and what it answered.

## Why behavior, and why like a test

An agent that answers well can still do the wrong thing on the way: cancel an order it never looked up, call a tool that failed and answer as if it worked, or delete an account because a message asked it to. Those are the defects that cost money, and they live in tool calls, not in the wording of the reply.

highbar checks them with deterministic assertions. No LLM grades the result, so the same agent behavior gives the same verdict, and a run can gate a merge with its exit code. Where an agent built on an LLM varies from run to run, a test runs several times and states how many must pass.

## A green that means something

A test ends in one of three states:

- **passed**: the agent did what the spec says, and highbar saw it do it;
- **failed**: the agent did something else;
- **error**: highbar could not verify it. The agent was down, the request failed, or no trace record shows the agent handled this message.

An `error` is never counted as a pass. A test that checks tool calls needs proof that the agent recorded this message, so an agent that was never instrumented, or a server left over on the port, cannot make it green.

## Who it is for

Teams that run an agent with tools in production and want a regression suite for its behavior: support and sales agents, internal assistants, anything that acts on a system through tools. The agent can be in Python, where a decorator records its tools, or in any language that writes the trace contract.

## What it is not

highbar does not grade free-form answers with an LLM, host a dashboard, or replace tools for prompt experiments and tracing in production. The [comparison](/comparison) sets it next to those tools.

Next: the [quick start](/getting-started/quickstart).
