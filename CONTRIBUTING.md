# Contributing to highbar

Bug reports, specs that should fail and don't, docs fixes and code are all welcome. The one thing this project cannot afford is a green result that does not mean the behavior was verified, so reports of that kind come first.

## Setup

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/vinimlo/highbar.git
cd highbar
uv sync --all-extras --dev
make test
```

## Before you open a pull request

```bash
make lint
uv run ruff format --check src/ tests/
make typecheck
make test
```

All four run in CI, on Python 3.12 and 3.13.

## How changes are made

- Branch from `develop` and open the pull request against `develop`. Use a prefix: `feat/`, `fix/`, `docs/`, `chore/`.
- Write the test first and watch it fail. A fix for a false green carries a regression test named after the case it covers.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`).
- The pull request says what changed and why, and shows it: the output before and after, copied from a real run.
- Messages are for the person reading a CI log: say what happened and what to do next.

`AGENTS.md` describes how a run works and where each piece lives.

## Reporting a bug

Include the spec (or the smallest one that shows the problem), the command, the full output, and what you expected instead. If an agent is involved, say whether highbar started it (`agent.start`) or attached to it.
