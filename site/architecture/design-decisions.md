# Design Decisions

This page explains the key design choices in Highbar and the reasoning behind them.

## 1. Why Python (Not TypeScript)

The original concept for Highbar considered TypeScript, but the implementation uses Python for a pragmatic reason: the SDK lives inside the agent's code, and the vast majority of AI agents are written in Python (LangChain, LlamaIndex, CrewAI, custom agents using the Anthropic/OpenAI SDKs).

Using Python for both the CLI and the SDK means:

- One language for the entire toolchain.
- No cross-language serialization or IPC between the test runner and the SDK.
- Agent developers do not need to add a Node.js dependency to their Python project.
- The decorator (`@hb.tool`) feels native and idiomatic.

## 2. Why a Decorator (Not Monkey-Patching)

Highbar uses an explicit `@hb.tool` decorator rather than monkey-patching agent framework internals or intercepting function calls at runtime.

**Advantages:**

- **Stable across SDK updates**: The decorator is self-contained and does not depend on the internal structure of LangChain, LlamaIndex, or any other framework.
- **Framework-agnostic**: Works with any agent framework, or no framework at all.
- **Explicit and visible**: Developers can see exactly which functions are instrumented by looking at the code.
- **One-time setup**: Add the decorator once; it works in all environments. No per-test configuration.

**Tradeoff:** The developer must add `@hb.tool` to each tool function. This is a deliberate choice -- Highbar instruments the functions you choose, not everything.

## 3. Why File Transport (Not HTTP)

Trace boxes are written to a local JSONL file rather than sent to an HTTP endpoint.

**Why file-based for MVP:**

- **Simpler**: No HTTP server to run alongside the test runner. No network configuration, ports, or firewall issues.
- **No middleware**: The `@hb.tool` decorator writes directly to the filesystem. No need for a sidecar process or HTTP client in the SDK.
- **Reliable**: File I/O is synchronous and straightforward. No retry logic, connection timeouts, or buffering concerns.
- **Debuggable**: The trace file is a plain text file that can be inspected with `cat`, `jq`, or any text editor.

**Future direction:** An HTTP sidecar transport is planned for remote agents (e.g., agents running in containers or cloud environments where the test runner cannot access the local filesystem).

## 4. Why No Mocks

Highbar deliberately does not provide mocking capabilities for tool functions or LLM responses.

**Rationale:** Highbar tests the real agent end-to-end. The entire point is to verify that the agent -- with its actual LLM, actual tools, and actual orchestration logic -- behaves correctly. Mocking any of these components defeats the purpose.

If a tool has side effects (sending emails, charging credit cards), the developer is responsible for configuring a test environment with sandboxed services. This is the same approach used in integration testing for web applications -- you use a test database, not a mocked one.

## 5. Why Checkpoint + Settle Time (Not Request IDs)

Most distributed tracing systems use correlation IDs to link requests to their traces. Highbar instead uses a simpler mechanism: line counting in the trace file plus a configurable settle time.

**Why this works:**

- Tests run sequentially, so there is never more than one trigger in flight.
- The trace file is append-only, so new traces always appear at the end.
- The settle time provides a buffer for traces written after the HTTP response returns.

**Why this is better for Highbar's use case:**

- **No middleware required**: The decorator is the only instrumentation. No need to thread request IDs through the agent's middleware stack, which would require changes to the agent's HTTP handler.
- **Framework-independent**: Works regardless of how the agent handles requests internally (sync, async, queued, etc.).
- **Simple mental model**: Before test, record position. After test, read new lines. Done.

## 6. YAGNI Choices

Several features that might seem like obvious additions were intentionally left out of the MVP:

| Not Included | Why |
|-------------|-----|
| **Dependency injection framework** | The `TestRunner` constructor accepts all dependencies explicitly. A DI container adds complexity without value at this scale. |
| **Plugin system** | Protocols provide extensibility. Adding a plugin registry, loader, and lifecycle management is premature. |
| **Abstract base classes** | Python `Protocol` classes (structural subtyping) are preferred over ABCs. No need to inherit from a base class -- just implement the methods. |
| **Event bus** | The `Reporter` protocol covers all notification needs. An event system would add indirection without new capability. |
| **Configuration file** | All configuration lives in the spec YAML. A separate config file would mean two places to look for settings. |
| **Parallel test execution** | Tests run sequentially because the checkpoint mechanism assumes a single trigger in flight. Parallel execution would require request ID correlation, which adds complexity to the SDK. |

Each of these decisions can be revisited as the project grows, but the current approach keeps the codebase small, understandable, and focused on the core value: testing agent behavior.
