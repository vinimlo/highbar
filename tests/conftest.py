"""What holds for the whole suite."""

from __future__ import annotations

import os

# Typer forces color when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set, and reads it
# once, when it is imported (typer.rich_utils.FORCE_TERMINAL). The color codes split an
# option's name into "-" and "-max-cost", so a test that looks for the name passed on
# every machine but GitHub's CI. pytest reads this file before any test imports typer,
# and every highbar the suite starts inherits the variable.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
