---
layout: home

hero:
  name: Highbar
  text: Behavior testing framework for AI agents
  tagline: Evals for AI agents that behave like tests.
  actions:
    - theme: brand
      text: Get Started
      link: /getting-started/introduction
    - theme: alt
      text: GitHub
      link: https://github.com/vinimlo/highbar

features:
  - title: Function Call Assertions
    details: Validate tool name, parameters, and return values. Subset matching by default — assert only what matters.
  - title: Multi-Turn Testing
    details: "Test complete conversational flows with state tracking between turns via <code v-pre>{{previous.*}}</code> templates."
  - title: Guardrail Validation
    details: Ensure your agent does NOT do what it shouldn't. Assert no tools called, reject injections, detect PII leakage.
  - title: YAML-First Specs
    details: Define expected behaviors declaratively in .highbar.yaml files. No test code to write.
  - title: Zero-Overhead SDK
    details: "@hb.tool decorator is a complete no-op in production. Zero dependencies when installed with pip install highbar[sdk]."
  - title: CI Ready
    details: Exit code 0/1 for pass/fail. Drop into GitHub Actions, GitLab CI, or any pipeline.
---
