"""Tokens and cost: a turn's usage priced, added up per level, and held against budgets.

Costs are added as decimals, for the reason ``resolve_runs`` works out fractions exactly:
0.1 + 0.2 in binary is more than 0.3, and a budget of 0.3 would fail on a sum equal to it.

A sum holds only what is known, and says what it left out. That is enough to fail a
budget, because tokens and cost only grow: known parts already over the limit prove it
was exceeded. Known parts under the limit prove nothing, so the check is unverified.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from typing import Any

from highbar._narrow import as_dict, as_list
from highbar.domain.models import Budget, Usage, UsageTotals

NO_USAGE = (
    "the {scope} reported no usage, so its budget cannot be checked; the agent must write "
    "`usage` in its `turn` record before the reply goes out (see the trace contract). If "
    "it does, raise agent.settle_time so the record is read in time"
)

UNATTRIBUTED_USAGE = (
    "cannot tell which test spent the usage of a `turn` record with no test context, and "
    "the {scope} keeps to its budget only without it. A thread the agent starts itself "
    "needs contextvars.copy_context() to keep the tag"
)


def _exact(value: float) -> Decimal:
    return Decimal(str(value))


def amount(value: float) -> str:
    """``value`` as plain digits: ``0.000012``, where ``str`` would give ``1.2e-05``."""
    return format(_exact(value), "f")


@dataclass(frozen=True)
class Price:
    """What one token costs, in the table's currency."""

    prompt: Decimal
    completion: Decimal
    input_cache_read: Decimal


def _key(name: str) -> str:
    """How a name is compared: `claude-sonnet-4.5` and `Claude-Sonnet-4-5` are one model.

    Providers write versions with hyphens where OpenRouter writes dots.
    """
    return name.strip().lower().replace(".", "-")


def _rate(pricing: Mapping[str, Any], key: str) -> Decimal | None:
    """A price per token, or None. OpenRouter lists a negative price for a variable one."""
    try:
        rate = Decimal(str(pricing.get(key)))
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate >= 0 else None


def read_prices(table: Mapping[str, Any]) -> dict[str, Price]:
    """The models of a price table that have a usable price, by normalized id."""
    prices: dict[str, Price] = {}
    for entry in as_list(table.get("data")) or []:
        model = as_dict(entry)
        pricing = as_dict(model.get("pricing")) if model is not None else None
        if model is None or pricing is None or not isinstance(model.get("id"), str):
            continue
        prompt, completion = _rate(pricing, "prompt"), _rate(pricing, "completion")
        if prompt is None or completion is None:
            continue
        # Without a cache price, cached tokens cost what the others do: a sum that errs
        # high, which is the safe side of a ceiling.
        cached = _rate(pricing, "input_cache_read")
        prices[_key(model["id"])] = Price(prompt, completion, prompt if cached is None else cached)
    return prices


@cache
def _table() -> tuple[str, dict[str, Price]]:
    text = resources.files("highbar.engine").joinpath("prices.json").read_text(encoding="utf-8")
    table = json.loads(text)
    return table["currency"], read_prices(table)


def find_price(usage: Usage, prices: Mapping[str, Price]) -> Price | None:
    """The price of ``usage.model``, by exact name: a near one may cost something else.

    A dated snapshot like `gpt-4o-2024-05-13` is not its alias: their prices have differed.
    A name that several providers use is unknown until ``provider`` says which.
    """
    if usage.model is None:
        return None
    model = _key(usage.model)
    if "/" in model:
        return prices.get(model)
    if usage.provider is not None:
        return prices.get(f"{_key(usage.provider)}/{model}")
    matches = [price for key, price in prices.items() if key.rpartition("/")[2] == model]
    return matches[0] if len(matches) == 1 else None


def priced(usage: Usage) -> tuple[Usage, str | None]:
    """``usage`` with its cost, and a warning when it should have one and cannot.

    The emitter's cost stands. Without one it comes from the price table, in the table's
    currency, and only from the whole count: a turn priced without its output tokens
    would look cheaper than it was, and pass a budget it may not have met.
    """
    if usage.cost is not None or usage.model is None:
        return usage, None
    currency, prices = _table()
    price = find_price(usage, prices)
    if price is None:
        return usage, (
            f"model '{usage.model}' is not in highbar's price table, so its turns have no "
            "cost; write `cost` and `currency` in the turn's usage"
        )
    if usage.input_tokens is None or usage.output_tokens is None:
        return usage, (
            f"model '{usage.model}' cannot be priced from a turn without input_tokens and "
            "output_tokens; write them, or `cost` and `currency`, in the turn's usage"
        )
    cached = min(usage.cached_input_tokens or 0, usage.input_tokens)
    cost = (
        (usage.input_tokens - cached) * price.prompt
        + cached * price.input_cache_read
        + usage.output_tokens * price.completion
    )
    return replace(usage, cost=float(cost), currency=currency), None


def _sum(values: Iterable[int | None]) -> int | None:
    """The sum of what is known; None when nothing is, which is not zero."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _whole(values: Iterable[int | None]) -> int | None:
    """The sum only when every part is known: a merged turn looks like one number."""
    parts = list(values)
    return None if any(part is None for part in parts) else _sum(parts)


def _shared(values: Iterable[str | None]) -> str | None:
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def merged(records: Sequence[Usage]) -> Usage:
    """The usage of one message, from each `turn` record of it that has some.

    One message can have several: the middleware's and one from ``hb.turn()``, or a
    writer that records each step. All of them were spent, so taking one would undercount
    and a budget could pass on it. A value the records do not agree on becomes unknown,
    and ``records`` keeps what each of them said: the sums and budgets read that.
    """
    if len(records) == 1:
        return records[0]
    currency = _shared(record.currency for record in records)
    costs = [record.cost for record in records]
    known = [cost for cost in costs if cost is not None]
    summable = currency is not None and len(known) == len(costs)
    return Usage(
        model=_shared(record.model for record in records),
        provider=_shared(record.provider for record in records),
        input_tokens=_whole(record.input_tokens for record in records),
        output_tokens=_whole(record.output_tokens for record in records),
        cached_input_tokens=_sum(record.cached_input_tokens for record in records),
        cost=float(sum(map(_exact, known), Decimal(0))) if summable else None,
        currency=currency if summable else None,
        llm_calls=_sum(record.llm_calls for record in records),
        records=tuple(records),
    )


def turn_usage(records: Sequence[Usage], unattributed: Sequence[Usage] = ()) -> Usage | None:
    """What one message reported. None when no record of it says anything readable.

    A record that could not be read is an empty ``Usage``: alone it is no usage, as for a
    writer with a `usage` of its own, and next to a readable one it makes the sum unknown.
    """
    own = merged(records) if any(record != Usage() for record in records) else None
    if not unattributed:
        return own
    return replace(own or Usage(), unattributed=tuple(unattributed))


def _records(usage: Usage | None) -> tuple[Usage, ...]:
    """What a turn's usage is made of. A sum cannot say what only some of its records knew."""
    if usage is None:
        return ()
    if usage.records:
        return usage.records
    return () if usage == Usage() else (usage,)


def has_cost(usage: Usage | None) -> bool:
    """Every record of a turn has a cost and its currency, so all of it can be counted."""
    records = _records(usage)
    return bool(records) and all(
        record.cost is not None and record.currency is not None for record in records
    )


def add_costs(*costs: Mapping[str, float]) -> dict[str, float]:
    """Sums per currency, added currency by currency."""
    sums: dict[str, Decimal] = {}
    for sum_of_currency in costs:
        for currency, cost in sum_of_currency.items():
            sums[currency] = sums.get(currency, Decimal(0)) + _exact(cost)
    return {currency: float(cost) for currency, cost in sums.items()}


def over_limit(costs: Mapping[str, float], limit: float) -> dict[str, float]:
    """The sums above ``limit``. Equal to it is within it."""
    return {currency: cost for currency, cost in costs.items() if _exact(cost) > _exact(limit)}


def _some(count: int, total: int, what: str) -> str:
    return f"{count} of {total} turn{'' if total == 1 else 's'} {what}"


def _lacking(turns: Sequence[Sequence[Usage]], lacks: Callable[[Usage], bool]) -> int:
    """How many ``turns`` have a record that ``lacks`` something."""
    return sum(any(lacks(record) for record in records) for records in turns)


def add_up(usages: Iterable[Usage | None]) -> UsageTotals | None:
    """What ``usages``, one per turn, add up to. None when no turn reported any.

    Each record of a turn counts on its own: its value may be unknown as a whole, and
    what its records do know was still spent. An agent that writes no usage owes no
    warning: nothing says it should, and an old trace has to read as it did. Once one
    turn reports it, every gap makes the sum partial.
    """
    turns = list(usages)
    reported = [records for records in map(_records, turns) if records]
    unattributed = sum(usage is not None and bool(usage.unattributed) for usage in turns)
    if not reported and not unattributed:
        return None
    spent = [record for records in reported for record in records]
    costs = add_costs(
        *(
            {record.currency: record.cost}
            for record in spent
            if record.cost is not None and record.currency is not None
        )
    )
    gaps = (
        (len(turns) - len(reported), "with no usage"),
        (_lacking(reported, lambda r: r.input_tokens is None), "with no input_tokens"),
        (_lacking(reported, lambda r: r.output_tokens is None), "with no output_tokens"),
        (_lacking(reported, lambda r: r.cost is None), "with no cost"),
        (
            _lacking(reported, lambda r: r.cost is not None and r.currency is None),
            "with a cost without a currency, left out of the sum",
        ),
        (unattributed, "next to usage with no test context, left out of the sum"),
    )
    warnings = [_some(count, len(turns), what) for count, what in gaps if count]
    if len(costs) > 1:
        warnings.append(f"costs in {', '.join(sorted(costs))}: added per currency, not together")
    return UsageTotals(
        turns=len(turns),
        input_tokens=_sum(record.input_tokens for record in spent),
        output_tokens=_sum(record.output_tokens for record in spent),
        cached_input_tokens=_sum(record.cached_input_tokens for record in spent),
        llm_calls=_sum(record.llm_calls for record in spent),
        cost=costs,
        complete=not warnings,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class BudgetCheck:
    """What a budget found: ``exceeded`` fails the test, ``unverified`` leaves it unchecked."""

    exceeded: list[str] = field(default_factory=list[str])
    unverified: list[str] = field(default_factory=list[str])


def check_budget(budget: Budget, usages: Sequence[Usage | None], scope: str) -> BudgetCheck:
    """Hold what ``usages`` add up to against ``budget``. ``scope`` names it in messages.

    Usage that no context ties to a test counts for none, like a call that has none. A
    budget that holds only without it is unverified: that usage may be this test's.
    """
    check = _held(budget, usages, scope)
    unattributed = [stray for usage in usages if usage is not None for stray in usage.unattributed]
    if unattributed and not (check.exceeded or check.unverified):
        counted = _held(budget, [*usages, *unattributed], scope)
        if counted.exceeded or counted.unverified:
            check.unverified.append(UNATTRIBUTED_USAGE.format(scope=scope))
    return check


def _held(budget: Budget, usages: Sequence[Usage | None], scope: str) -> BudgetCheck:
    totals = add_up(usages)
    if totals is None:
        return BudgetCheck(unverified=[NO_USAGE.format(scope=scope)])
    check = BudgetCheck()
    if budget.max_input_tokens is not None:
        _check_input_tokens(budget.max_input_tokens, usages, totals, scope, check)
    if budget.max_cost is not None:
        _check_cost(budget.max_cost, usages, totals, scope, check)
    return check


def _check_input_tokens(
    limit: int,
    usages: Sequence[Usage | None],
    totals: UsageTotals,
    scope: str,
    check: BudgetCheck,
) -> None:
    used = totals.input_tokens or 0
    if used > limit:
        cached = f" ({totals.cached_input_tokens} of them cached)"
        detail = cached if totals.cached_input_tokens else ""
        check.exceeded.append(
            f"the {scope} used {used} input tokens{detail}, over max_input_tokens {limit}"
        )
        return
    missing = sum(usage is None or usage.input_tokens is None for usage in usages)
    if missing:
        check.unverified.append(
            f"cannot check max_input_tokens {limit}: {_some(missing, len(usages), 'without')} "
            "input_tokens; the agent must write it in the usage of each `turn` record"
        )


def _check_cost(
    limit: float,
    usages: Sequence[Usage | None],
    totals: UsageTotals,
    scope: str,
    check: BudgetCheck,
) -> None:
    over = over_limit(totals.cost, limit)
    if over:
        spent = ", ".join(f"{amount(cost)} {currency}" for currency, cost in sorted(over.items()))
        check.exceeded.append(f"the {scope} cost {spent}, over max_cost {amount(limit)}")
        return
    missing = sum(not has_cost(usage) for usage in usages)
    if missing:
        check.unverified.append(
            f"cannot check max_cost {amount(limit)}: {_some(missing, len(usages), 'without')} "
            "a cost and its currency; write both in the turn's usage, or a `model` from "
            "highbar's price table with its tokens"
        )
    elif len(totals.cost) > 1:
        check.unverified.append(
            f"cannot check max_cost {amount(limit)}: costs in "
            f"{', '.join(sorted(totals.cost))} cannot be added; write one currency"
        )
