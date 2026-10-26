#!/usr/bin/env bash
# Install a built wheel in a fresh environment outside the checkout, and run the
# quickstart with it: the package as PyPI will serve it, not the source tree.
#
#     scripts/smoke_wheel.sh dist/highbar-0.1.0rc1-py3-none-any.whl
set -euo pipefail

wheel="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv venv -q --python "${PYTHON_VERSION:-3.12}" "$work/venv"
VIRTUAL_ENV="$work/venv" uv pip install -q "${wheel}[quickstart]"
export PATH="$work/venv/bin:$PATH"

mkdir "$work/project"
cd "$work/project"
highbar --version
highbar init
highbar run
