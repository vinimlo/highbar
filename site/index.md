---
layout: home

hero:
  name: highbar
  text: Evals for AI agents that behave like tests
  tagline: Check what your agent does, not only what it says. A YAML spec, the agent you ship, and an exit code that gates the merge.
  actions:
    - theme: brand
      text: Quick start
      link: /getting-started/quickstart
    - theme: alt
      text: Why highbar
      link: /getting-started/introduction
    - theme: alt
      text: GitHub
      link: https://github.com/vinimlo/highbar

features:
  - title: Tool calls, checked
    details: Which tools, in which order, with which arguments, and nothing else. A call that failed does not count as done.
  - title: Guardrails that cannot pass by accident
    details: Forbidden tools and forbidden text. A test with no proof the agent saw its message is an error, never a pass.
  - title: Deterministic verdicts
    details: No LLM judges the result. Repeat a test with runs and min_pass where the agent is not.
  - title: The agent you deploy
    details: highbar starts your agent or attaches to one already running, with your auth, your request shape and a fresh session per test.
  - title: Any language
    details: A Python SDK records tools with a decorator. Any other agent writes the trace contract, one JSON line per call.
  - title: Made for CI
    details: Exit codes 0, 1 and 2. A failure says what the agent did instead.
---
