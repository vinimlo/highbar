"""Exact tests and intervals for pass counts, on the standard library alone.

Counts are whole numbers, so the exact tests compare integer weights: no
floating-point tolerance decides which tables are "as extreme".
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence

Z_95 = 1.959963984540054
EXACT_LIMIT = 200_000
"""Most tables the exact heterogeneity test enumerates before it uses chi-square."""


def wilson(passes: int, total: int) -> tuple[float, float]:
    """The Wilson 95% interval for a pass rate; (0, 1) when nothing ran."""
    if total == 0:
        return 0.0, 1.0
    rate = passes / total
    z2 = Z_95 * Z_95
    centre = (rate + z2 / (2 * total)) / (1 + z2 / total)
    half = Z_95 * math.sqrt(rate * (1 - rate) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def fisher_exact(passes_a: int, total_a: int, passes_b: int, total_b: int) -> float:
    """Two-sided Fisher exact test on two pass counts."""
    return rounds_disagree_p([(passes_a, total_a), (passes_b, total_b)])


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values, in the order given."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [1.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def welch_p(base: Sequence[float], head: Sequence[float]) -> float:
    """Welch's two-sided t-test on the rounds' pass rates: each round is one value.

    Executions in a round are not independent when the agent's choice sticks within it,
    so the rounds are what repeats (HB-45). With no spread on either side, equal means
    give 1 and different means give 0: the rounds cannot tell, and the pooled count
    is left to decide.
    """
    if len(base) < 2 or len(head) < 2:
        return 1.0
    mean_b, mean_h = sum(base) / len(base), sum(head) / len(head)
    var_b = sum((x - mean_b) ** 2 for x in base) / (len(base) - 1)
    var_h = sum((x - mean_h) ** 2 for x in head) / (len(head) - 1)
    part_b, part_h = var_b / len(base), var_h / len(head)
    spread = part_b + part_h
    if spread < 1e-12:  # rates of whole counts: below this it is rounding, not spread
        return 1.0 if math.isclose(mean_b, mean_h) else 0.0
    t = (mean_h - mean_b) / math.sqrt(spread)
    df = spread**2 / (part_b**2 / (len(base) - 1) + part_h**2 / (len(head) - 1))
    return t_two_sided(t, df)


def t_two_sided(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student's t with ``df`` degrees of freedom."""
    return _beta_regularized(df / (df + t * t), df / 2, 0.5)


def _beta_regularized(x: float, a: float, b: float) -> float:
    """The regularized incomplete beta I_x(a, b), by its continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(x, a, b) / a
    return 1 - front * _beta_fraction(1 - x, b, a) / b


def _beta_fraction(x: float, a: float, b: float) -> float:
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 10_000):
        for numerator in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 + numerator * d
            d = 1 / (tiny if abs(d) < tiny else d)
            c = 1 + numerator / c
            c = tiny if abs(c) < tiny else c
            h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return h


def rounds_disagree_p(rounds: Sequence[tuple[int, int]]) -> float:
    """The p-value that the rounds share one pass rate (rounds × pass/fail).

    Exact (Freeman-Halton) while the tables to enumerate are few, chi-square
    beyond that. A small p means the rounds disagree more than chance explains.
    """
    kept = [(passes, total) for passes, total in rounds if total > 0]
    passes = sum(p for p, _ in kept)
    total = sum(n for _, n in kept)
    if len(kept) < 2 or passes in (0, total):
        return 1.0
    if math.prod(n + 1 for _, n in kept) <= EXACT_LIMIT:
        return _exact(kept, passes, total)
    return _chi_square(kept, passes, total)


def _exact(rounds: list[tuple[int, int]], passes: int, total: int) -> float:
    sizes = [n for _, n in rounds]
    observed = math.prod(math.comb(n, p) for p, n in rounds)
    extreme = sum(w for w in _weights(sizes, passes) if w <= observed)
    return min(1.0, extreme / math.comb(total, passes))


def _weights(sizes: list[int], passes: int) -> Iterator[int]:
    """prod C(n_i, x_i) for every split of `passes` over rounds of these sizes."""
    if len(sizes) == 1:
        if passes <= sizes[0]:
            yield math.comb(sizes[0], passes)
        return
    first, rest = sizes[0], sizes[1:]
    room = sum(rest)
    for x in range(max(0, passes - room), min(first, passes) + 1):
        head = math.comb(first, x)
        for tail in _weights(rest, passes - x):
            yield head * tail


def _chi_square(rounds: list[tuple[int, int]], passes: int, total: int) -> float:
    rate = passes / total
    statistic = 0.0
    for p, n in rounds:
        expected_pass, expected_fail = n * rate, n * (1 - rate)
        statistic += (p - expected_pass) ** 2 / expected_pass
        statistic += (n - p - expected_fail) ** 2 / expected_fail
    return _upper_gamma((len(rounds) - 1) / 2, statistic / 2)


def _upper_gamma(a: float, x: float) -> float:
    """The regularized upper incomplete gamma Q(a, x): the chi-square tail."""
    if x <= 0:
        return 1.0
    log_front = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1:
        term = total = 1 / a
        k = a
        while abs(term) > abs(total) * 1e-15:
            k += 1
            term *= x / k
            total += term
        return max(0.0, 1 - total * math.exp(log_front))
    tiny = 1e-300
    b = x + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    i = 1
    while True:
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        step = d * c
        h *= step
        if abs(step - 1) < 1e-15 or i > 10_000:
            break
        i += 1
    return min(1.0, math.exp(log_front) * h)
