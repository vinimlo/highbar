"""The statistics behind `highbar compare`, against known values (HB-5)."""

from __future__ import annotations

import pytest

from highbar.compare.stats import fisher_exact, holm, rounds_disagree_p, wilson


def test_fisher_his_warranty_case_is_0_048() -> None:
    # 1/5 against 5/5, the sink's warranty test on 30/10 (his comment on HB-5).
    assert fisher_exact(1, 5, 5, 5) == pytest.approx(0.0476, abs=1e-4)


def test_fisher_is_symmetric_and_one_when_equal() -> None:
    assert fisher_exact(5, 5, 1, 5) == pytest.approx(fisher_exact(1, 5, 5, 5))
    assert fisher_exact(3, 5, 3, 5) == pytest.approx(1.0)
    assert fisher_exact(0, 0, 3, 5) == pytest.approx(1.0)


def test_fisher_a_large_difference() -> None:
    # 0/20 against 20/20: 2 / C(40, 20).
    assert fisher_exact(0, 20, 20, 20) == pytest.approx(2 / 137846528820, rel=1e-6)


def test_wilson_matches_his_comment() -> None:
    low, high = wilson(1, 5)
    assert (round(low, 3), round(high, 3)) == (0.036, 0.624)
    low, high = wilson(5, 5)
    assert (round(low, 3), round(high, 3)) == (0.566, 1.0)


def test_wilson_with_no_executions_says_nothing() -> None:
    assert wilson(0, 0) == (0.0, 1.0)


def test_holm_his_seven_tests() -> None:
    adjusted = holm([0.048, 0.6, 1.0, 1.0, 0.5, 0.9, 0.7])
    assert adjusted[0] == pytest.approx(0.336)


def test_holm_is_monotone_and_capped_at_one() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5, 0.6]) == pytest.approx([1.0, 1.0])
    assert holm([]) == []


def test_rounds_disagree_exact_for_two_rounds() -> None:
    # Two rounds of 5 at 1 and 5 passes: the 2x2 Fisher case again.
    assert rounds_disagree_p([(1, 5), (5, 5)]) == pytest.approx(0.0476, abs=1e-4)


def test_rounds_disagree_bimodal_four_rounds() -> None:
    assert rounds_disagree_p([(1, 5), (5, 5), (1, 5), (5, 5)]) < 0.01


def test_rounds_that_agree() -> None:
    assert rounds_disagree_p([(4, 5), (5, 5), (4, 5), (5, 5)]) > 0.5
    assert rounds_disagree_p([(5, 5), (5, 5)]) == pytest.approx(1.0)


def test_rounds_disagree_large_counts_use_chi_square() -> None:
    # 10 rounds of 50: too many tables to enumerate. 25/50 every round agrees;
    # 0/50 and 50/50 alternating does not.
    assert rounds_disagree_p([(25, 50)] * 10) == pytest.approx(1.0)
    assert rounds_disagree_p([(0, 50), (50, 50)] * 5) < 1e-6


def test_welch_rounds_known_value() -> None:
    # scipy.stats.ttest_ind([1, 2, 3], [4, 5, 6], equal_var=False).pvalue
    from highbar.compare.stats import welch_p

    assert welch_p([1, 2, 3], [4, 5, 6]) == pytest.approx(0.021311641128756727, rel=1e-6)


@pytest.mark.parametrize(
    ("t", "df"), [(12.7062047, 1), (2.7764451, 4), (2.2281389, 10), (2.0, 3.0)]
)
def test_student_t_tail_against_the_table(t: float, df: float) -> None:
    from highbar.compare.stats import t_two_sided

    expected = {1: 0.05, 4: 0.05, 10: 0.05, 3.0: 0.1393}[df]
    assert t_two_sided(t, df) == pytest.approx(expected, abs=2e-4)


def test_welch_rounds_with_no_spread() -> None:
    from highbar.compare.stats import welch_p

    assert welch_p([1.0, 1.0, 1.0], [1.0, 1.0, 1.0]) == 1.0
    # No spread on either side and different means: the rounds alone cannot tell
    # chance from a difference, so they claim one; the pooled count decides.
    assert welch_p([1.0, 1.0, 1.0], [0.8, 0.8, 0.8]) == 0.0
