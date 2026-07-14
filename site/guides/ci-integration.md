# CI/CD Integration

Gauntlet is designed to run in continuous integration pipelines. It uses standard exit codes and produces terminal output compatible with CI log viewers.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All tests passed |
| `1` | One or more tests failed, or no spec files were found |

## GitHub Actions

### Basic Setup

```yaml
name: Agent Behavior Tests
on:
  push:
    paths: ["prompts/**", "specs/**"]
  pull_request:

jobs:
  gauntlet:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install gauntlet

      - run: gauntlet run
```

This workflow triggers on changes to prompt templates or spec files, ensuring behavior tests run whenever the agent's instructions or expected behaviors change.

### With API Keys and Environment Variables

Most agents need API keys or other configuration. Pass these as environment variables using GitHub secrets:

```yaml
name: Agent Behavior Tests
on:
  push:
    paths: ["prompts/**", "specs/**", "src/**"]
  pull_request:

jobs:
  gauntlet:
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: $&#123;&#123; secrets.ANTHROPIC_API_KEY &#125;&#125;
      OPENAI_API_KEY: $&#123;&#123; secrets.OPENAI_API_KEY &#125;&#125;
      DATABASE_URL: "sqlite:///test.db"

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install gauntlet
          pip install -r requirements.txt

      - name: Run behavior tests
        run: gauntlet run

      - name: Run guardrail tests only
        run: gauntlet run specs/guardrails.gauntlet.yaml
```

### With uv

If your project uses `uv` for dependency management:

```yaml
jobs:
  gauntlet:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install uv
        run: pip install uv

      - name: Install dependencies
        run: uv sync

      - name: Run behavior tests
        run: uv run gauntlet run
```

## GitLab CI

```yaml
gauntlet:
  image: python:3.12
  stage: test
  script:
    - pip install gauntlet
    - pip install -r requirements.txt
    - gauntlet run
  rules:
    - changes:
        - prompts/**
        - specs/**
        - src/**
```

## Running Specific Specs

You can run a single spec file or let Gauntlet discover all specs in the `specs/` directory:

```bash
# Run all specs (discovers *.gauntlet.yaml in specs/)
gauntlet run

# Run a specific spec file
gauntlet run specs/orders.gauntlet.yaml
```

This lets you split your CI pipeline into separate jobs for different spec files, or run guardrail tests separately from functional tests.

## Tips

### Trigger on the Right File Changes

Configure your CI to run behavior tests when relevant files change:

- **`prompts/**`** -- system prompts and prompt templates
- **`specs/**`** -- the spec files themselves
- **`src/**`** -- agent source code

This avoids running slow behavior tests on unrelated changes like documentation updates.

### Store API Keys as Secrets

Never hardcode API keys in spec files or CI configuration. Use your CI platform's secrets management:

- GitHub Actions: Repository secrets (use `secrets.KEY_NAME` in workflow YAML)
- GitLab CI: CI/CD variables (masked)
- CircleCI: Environment variables or contexts

### Set Up a Test Environment

Configure your agent to use test-specific settings in CI:

- Use a test database or in-memory storage
- Point to sandbox API endpoints
- Reduce timeouts for faster feedback

### Validate Specs Before Running

Add a validation step to catch YAML syntax errors before attempting to run tests:

```yaml
- name: Validate specs
  run: |
    for spec in specs/*.gauntlet.yaml; do
      gauntlet validate "$spec"
    done

- name: Run behavior tests
  run: gauntlet run
```

### Separate Functional and Guardrail Tests

Consider running guardrail tests on every push and functional tests only on pull requests:

```yaml
jobs:
  guardrails:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install gauntlet && pip install -r requirements.txt
      - run: gauntlet run specs/guardrails.gauntlet.yaml

  functional:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install gauntlet && pip install -r requirements.txt
      - run: gauntlet run specs/functional.gauntlet.yaml
```
