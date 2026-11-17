"""What every check of a non-tool record type shares (HB-3, HB-11; decision E1)."""

from __future__ import annotations


class RecordNotObserved(Exception):
    """No record of the type for the test: its check could not run, which is an `error`
    and never a pass (decision E2)."""
