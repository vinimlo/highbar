"""--max-errors: executions in a row that failed for one cause of the infrastructure (HB-34).

An endpoint that answers 500 to every message turns each execution of a run into an
error, one after the other, for as long as the run lasts, and with a paid LLM behind it,
at a cost. Counting them in a row, for one cause, tells that from an agent that fails
now and then, which is what min_pass is for.
"""

from __future__ import annotations

from dataclasses import dataclass

from highbar.adapters.http_client import InfraCause

# What --max-errors is when not given. 0 turns the stop off.
DEFAULT_MAX_ERRORS = 5

# The code the CLI exits with after this stop (EXIT_REPEATED_ERRORS), in the message.
EXIT_CODE = 4


@dataclass
class ErrorStreak:
    """The executions in a row that ended in an error of one ``InfraCause``.

    One for the whole run: the runner of each spec adds to the same one, so the count
    goes on across tests and specs.
    """

    count: int = 0
    cause: InfraCause | None = None

    def add(self, cause: InfraCause | None) -> int:
        """One more execution: the cause of its error, or None for any other ending.

        Anything else (a pass, a failure, an error of another kind) starts over at 0;
        another cause starts over at 1. Returns the count.
        """
        if cause is None:
            self.count, self.cause = 0, None
        elif cause == self.cause:
            self.count += 1
        else:
            self.count, self.cause = 1, cause
        return self.count


def stop_message(count: int, cause: InfraCause, test: str) -> str:
    """The one line that says why the run stopped: no body, and masked by the caller."""
    executions = "1 execution" if count == 1 else f"{count} executions in a row"
    return (
        f"{executions} failed {cause.phrase()} (last: '{test}'); the infrastructure "
        f"failed, not the agent, so the run stopped. Exits with {EXIT_CODE}."
    )
