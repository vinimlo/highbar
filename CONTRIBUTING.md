# Contributing to Gauntlet

Thanks for your interest in contributing! Gauntlet is at an early stage, so every contribution — code, docs, ideas, bug reports — has an outsized impact.

## Prerequisites

- **Node.js** >= 20
- **pnpm** (recommended) or npm
- A code editor with TypeScript support

## Getting Started

```bash
git clone https://github.com/your-org/gauntlet.git
cd gauntlet
pnpm install
pnpm dev      # watch mode
pnpm test     # run tests
pnpm lint     # biome lint + format check
```

## Project Structure (Planned)

```
src/
├── cli/            # CLI entry point, argument parsing, command routing
├── parser/         # YAML spec parser and validator
├── runner/         # Test execution engine, agent communication
├── assertions/     # Assertion types (text, regex, function_calls, response)
├── variations/     # Semantic variation generator (LLM-powered paraphrasing)
├── reporters/      # Output formatters (terminal, JSON, HTML)
└── presets/        # Provider presets (openai, anthropic, langchain)
```

### Key Concepts

- **Spec**: A `.gauntlet.yaml` file defining agent behavior tests
- **Suite**: A named group of tests within a spec
- **Test**: A single trigger → expect pair, or a multi-turn `turns:` flow
- **Preset**: A provider adapter that knows how to parse tool calls from a specific API format
- **Assertion**: A rule that validates agent output (response text, function calls, etc.)

## Development Workflow

### Branch Naming

```
feat/short-description    # new features
fix/short-description     # bug fixes
docs/short-description    # documentation
refactor/short-description
```

### Commits

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add anthropic preset
fix: handle empty function_calls array
docs: add multi-turn testing guide
test: add guardrail assertion tests
refactor: extract variation generator interface
```

### Pull Requests

1. Fork the repo and create a feature branch
2. Write tests for new functionality
3. Ensure `pnpm test` and `pnpm lint` pass
4. Submit a PR with a clear description of what and why

## Extending Gauntlet

### Adding a New Preset

Presets live in `src/presets/`. Each preset implements the `Preset` interface:

```typescript
// src/presets/types.ts
export interface Preset {
  name: string;
  parseResponse(raw: unknown): ParsedResponse;
  buildRequest(trigger: string, context?: TurnContext): RequestPayload;
}
```

Create a new file (e.g., `src/presets/custom.ts`), implement the interface, and register it in `src/presets/index.ts`.

### Adding a New Assertion Type

Assertions live in `src/assertions/`. Each assertion implements:

```typescript
// src/assertions/types.ts
export interface Assertion {
  name: string;
  evaluate(actual: ParsedResponse, expected: ExpectBlock): AssertionResult;
}
```

Example: to add a `latency` assertion that checks response time, create `src/assertions/latency.ts` and register it in the assertion registry.

### Adding a New Reporter

Reporters live in `src/reporters/`. Implement the `Reporter` interface:

```typescript
export interface Reporter {
  onSuiteStart(suite: Suite): void;
  onTestResult(test: Test, result: TestResult): void;
  onSuiteEnd(suite: Suite, results: TestResult[]): void;
  onRunEnd(summary: RunSummary): void;
}
```

## Code Style

- **TypeScript strict mode** — no `any`, no implicit returns
- **Biome** for linting and formatting (runs in CI)
- **Vitest** for unit and integration tests
- Prefer descriptive names over comments
- Keep functions small and focused

## Questions?

Open an issue or start a discussion. We're friendly.
