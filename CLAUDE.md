# CLAUDE.md — Gauntlet

## Overview

Gauntlet is an open-source behavior testing framework for AI agents — "Playwright for AI Agents". It tests the **agent** (not the LLM) by defining expected behaviors in YAML specs and running them against the agent's endpoint.

## Tech Stack

- **Language**: TypeScript (strict mode)
- **Package manager**: pnpm
- **Testing**: Vitest
- **Linting/Formatting**: Biome
- **Build**: tsup (ESM + CJS)
- **CLI framework**: citty or commander

## Commands

```bash
pnpm dev        # watch mode (development)
pnpm build      # build CLI for distribution
pnpm test       # run unit + integration tests
pnpm lint       # biome check (lint + format)
pnpm lint:fix   # biome check --write
```

## Architecture

```
YAML Spec → Parser → Runner → Assertions → Reporter
                       ↓
                  Agent Endpoint
                  (via Preset adapter)
```

1. **Parser** reads `.gauntlet.yaml` files, validates schema, resolves `{{previous.*}}` templates
2. **Runner** orchestrates test execution — sends triggers to the agent endpoint, collects responses
3. **Presets** (openai, anthropic, langchain) adapt request/response formats per provider
4. **Assertions** evaluate parsed responses against `expect:` blocks (text, regex, function_calls)
5. **Variations** auto-generate semantic paraphrases of triggers via LLM
6. **Reporters** format output (terminal, JSON, HTML)

## Key Conventions

- Spec files use `.gauntlet.yaml` extension
- Config file: `gauntlet.config.yaml`
- Default spec directory: `specs/`
- Presets implement the `Preset` interface (see `src/presets/types.ts`)
- Assertions implement the `Assertion` interface (see `src/assertions/types.ts`)
- Multi-turn state is threaded via `{{previous.*}}` template syntax
- Tests that assert `function_calls: []` are guardrail/negative tests

## Project Structure

```
src/
├── cli/            # Entry point, command routing
├── parser/         # YAML spec parser + validator
├── runner/         # Test execution engine
├── assertions/     # text, regex, function_calls, response
├── variations/     # LLM-powered semantic paraphrase generator
├── reporters/      # terminal, json, html formatters
└── presets/        # openai, anthropic, langchain adapters
specs/
└── examples/       # Example .gauntlet.yaml files
pitch/              # Original pitch deck (AgentSpec branding, kept for reference)
```

## Current Status

**Pre-implementation** — documentation and spec design phase. No source code yet. The `README.md`, `CONTRIBUTING.md`, and example specs define the target API surface.
