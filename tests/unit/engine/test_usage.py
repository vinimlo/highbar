"""Tokens and cost: priced, added up, and held against a budget."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources

import pytest

from highbar.domain.models import Budget, Usage
from highbar.engine.usage import (
    Price,
    add_costs,
    add_up,
    amount,
    check_budget,
    find_price,
    merged,
    over_limit,
    priced,
    read_prices,
)

MINI = {"model": "gpt-4o-mini", "provider": "openai"}


def _usd(cost: float, **fields: int) -> Usage:
    return Usage(input_tokens=100, output_tokens=10, cost=cost, currency="USD", **fields)


class TestPriced:
    def test_ac3_the_emitters_cost_stands(self) -> None:
        usage = Usage(**MINI, input_tokens=1000, output_tokens=200, cost=9.0, currency="EUR")

        assert priced(usage) == (usage, None)

    def test_ac3_a_known_model_is_priced_from_the_table(self) -> None:
        usage = Usage(**MINI, input_tokens=1000, output_tokens=200)

        result, warning = priced(usage)

        # 1000 * 0.15 + 200 * 0.60, per million tokens.
        assert (result.cost, result.currency) == (0.00027, "USD")
        assert warning is None

    def test_ac3_cached_tokens_take_the_cached_price(self) -> None:
        usage = Usage(**MINI, input_tokens=1000, output_tokens=200, cached_input_tokens=800)

        result, _ = priced(usage)

        # 200 * 0.15 + 800 * 0.075 + 200 * 0.60: cached tokens are not counted twice.
        assert result.cost == 0.00021

    def test_ac3_an_unknown_model_has_no_cost_and_a_warning(self) -> None:
        usage = Usage(model="my-model", input_tokens=1000, output_tokens=200)

        result, warning = priced(usage)

        assert result.cost is None
        assert warning is not None
        assert "my-model" in warning
        assert "price table" in warning

    @pytest.mark.parametrize("tokens", [{"input_tokens": 5}, {"output_tokens": 5}, {}])
    def test_a_partial_count_is_not_priced(self, tokens: dict[str, int]) -> None:
        """A price on part of the tokens would look cheap, and pass a budget it may not meet."""
        result, warning = priced(Usage(**MINI, **tokens))

        assert result.cost is None
        assert warning is not None

    def test_no_model_says_nothing_here(self) -> None:
        usage = Usage(input_tokens=5, output_tokens=5)

        assert priced(usage) == (usage, None)

    def test_the_tables_currency_replaces_one_sent_without_a_cost(self) -> None:
        result, _ = priced(Usage(**MINI, input_tokens=1, output_tokens=1, currency="BRL"))

        assert result.currency == "USD"


class TestFindPrice:
    PRICE = Price(Decimal(1), Decimal(2), Decimal(1))
    OTHER = Price(Decimal(3), Decimal(4), Decimal(3))
    PRICES = {"openai/gpt-x": PRICE, "acme/llama-3": PRICE, "meta/llama-3": OTHER}

    def test_a_bare_name_of_one_provider(self) -> None:
        assert find_price(Usage(model="gpt-x"), self.PRICES) is self.PRICE

    def test_provider_and_model(self) -> None:
        usage = Usage(model="llama-3", provider="meta")

        assert find_price(usage, self.PRICES) is self.OTHER

    def test_a_full_id_in_the_model(self) -> None:
        usage = Usage(model="meta/llama-3", provider="openrouter")

        assert find_price(usage, self.PRICES) is self.OTHER

    def test_a_name_two_providers_use_is_unknown_without_a_provider(self) -> None:
        assert find_price(Usage(model="llama-3"), self.PRICES) is None

    def test_a_provider_outside_the_table_is_unknown(self) -> None:
        usage = Usage(model="gpt-x", provider="azure")

        assert find_price(usage, self.PRICES) is None

    def test_a_dated_snapshot_is_not_its_alias(self) -> None:
        """They have cost different amounts: a near name must not price a turn."""
        assert find_price(Usage(model="gpt-x-2024-05-13"), self.PRICES) is None

    def test_case_and_version_separators_do_not_matter(self) -> None:
        entry = {"id": "anthropic/claude-sonnet-4.5", "pricing": {"prompt": "1", "completion": "2"}}

        prices = read_prices({"data": [entry]})

        assert find_price(Usage(model="Claude-Sonnet-4-5"), prices) is not None


class TestReadPrices:
    def test_the_shape_of_openrouters_list(self) -> None:
        table = {
            "data": [
                {
                    "id": "openai/gpt-x",
                    "name": "ignored",
                    "pricing": {
                        "prompt": "0.000001",
                        "completion": "0.000002",
                        "request": "0",
                        "input_cache_read": "0.0000001",
                    },
                }
            ]
        }

        (price,) = read_prices(table).values()

        assert price == Price(Decimal("0.000001"), Decimal("0.000002"), Decimal("0.0000001"))

    def test_without_a_cache_price_cached_tokens_cost_the_same(self) -> None:
        table = {"data": [{"id": "a/b", "pricing": {"prompt": "1", "completion": "2"}}]}

        (price,) = read_prices(table).values()

        assert price.input_cache_read == price.prompt

    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
            {"id": "a/b", "pricing": {"prompt": "1"}},
            {"id": "a/b", "pricing": {"prompt": "free", "completion": "1"}},
            {"id": "a/b", "pricing": {"prompt": "NaN", "completion": "1"}},
            {"id": "a/b"},
            {"pricing": {"prompt": "1", "completion": "1"}},
            "not an entry",
        ],
    )
    def test_a_model_without_a_usable_price_stays_out(self, entry: object) -> None:
        assert read_prices({"data": [entry]}) == {}

    def test_every_model_in_the_shipped_table_has_a_usable_price(self) -> None:
        text = resources.files("highbar.engine").joinpath("prices.json").read_text(encoding="utf-8")
        table = json.loads(text)

        assert table["currency"] == "USD"
        assert len(read_prices(table)) == len(table["data"])


class TestAddUp:
    def test_nothing_reported_is_no_sum_and_no_warning(self) -> None:
        assert add_up([None, None]) is None
        assert add_up([]) is None

    def test_ac4_a_sum_of_two_turns(self) -> None:
        totals = add_up(
            [
                Usage(
                    input_tokens=1000,
                    output_tokens=100,
                    cached_input_tokens=800,
                    cost=0.01,
                    currency="USD",
                    llm_calls=2,
                ),
                Usage(input_tokens=500, output_tokens=50, cost=0.02, currency="USD", llm_calls=1),
            ]
        )

        assert totals is not None
        assert totals.turns == 2
        assert (totals.input_tokens, totals.output_tokens) == (1500, 150)
        assert (totals.cached_input_tokens, totals.llm_calls) == (800, 3)
        assert totals.cost == {"USD": 0.03}
        assert (totals.complete, totals.warnings) == (True, ())

    def test_a_cost_is_added_exactly(self) -> None:
        totals = add_up([_usd(0.1), _usd(0.2)])

        assert totals is not None
        assert totals.cost == {"USD": 0.3}  # 0.1 + 0.2 in binary is 0.30000000000000004

    def test_ac5_a_turn_without_usage_makes_the_sum_partial(self) -> None:
        totals = add_up([_usd(0.1), None])

        assert totals is not None
        assert totals.turns == 2
        assert totals.input_tokens == 100
        assert totals.cost == {"USD": 0.1}
        assert totals.complete is False
        assert totals.warnings == ("1 of 2 turns with no usage",)

    def test_ac5_a_missing_cost_leaves_the_known_parts(self) -> None:
        totals = add_up([_usd(0.1), Usage(input_tokens=5, output_tokens=5)])

        assert totals is not None
        assert totals.input_tokens == 105
        assert totals.cost == {"USD": 0.1}
        assert totals.complete is False
        assert totals.warnings == ("1 of 2 turns with no cost",)

    def test_ac5_a_cost_without_a_currency_is_left_out(self) -> None:
        unknown = Usage(input_tokens=5, output_tokens=5, cost=5.0)

        totals = add_up([_usd(0.1), unknown])

        assert totals is not None
        assert totals.cost == {"USD": 0.1}
        assert totals.complete is False
        assert "a cost without a currency" in totals.warnings[0]

    def test_ac5_currencies_are_added_apart(self) -> None:
        euro = Usage(input_tokens=5, output_tokens=5, cost=0.5, currency="EUR")

        totals = add_up([_usd(0.1), _usd(0.2), euro])

        assert totals is not None
        assert totals.cost == {"USD": 0.3, "EUR": 0.5}
        assert totals.complete is False
        assert totals.warnings == ("costs in EUR, USD: added per currency, not together",)

    def test_a_count_nobody_reported_is_unknown_not_zero(self) -> None:
        totals = add_up([Usage(cost=1.0, currency="USD")])

        assert totals is not None
        assert (totals.input_tokens, totals.llm_calls) == (None, None)
        assert "no input_tokens" in " ".join(totals.warnings)

    def test_cached_tokens_and_calls_are_not_gaps(self) -> None:
        totals = add_up([_usd(0.1)])

        assert totals is not None
        assert totals.complete is True


class TestMerged:
    def test_one_record_is_itself(self) -> None:
        usage = _usd(0.1)

        assert merged([usage]) is usage

    def test_ac6_several_records_of_a_message_are_added_up(self) -> None:
        usage = merged(
            [
                Usage(model="m", input_tokens=10, output_tokens=1, cost=0.1, currency="USD"),
                Usage(model="m", input_tokens=20, output_tokens=2, cost=0.2, currency="USD"),
            ]
        )

        assert usage == Usage(model="m", input_tokens=30, output_tokens=3, cost=0.3, currency="USD")

    def test_a_value_the_records_disagree_on_is_unknown(self) -> None:
        usage = merged(
            [
                Usage(model="a", cost=0.1, currency="USD"),
                Usage(model="b", cost=0.2, currency="EUR"),
            ]
        )

        assert (usage.model, usage.cost, usage.currency) == (None, None, None)

    def test_a_partial_part_leaves_the_turn_unknown_and_never_short(self) -> None:
        """A sum of the known part would read as the whole turn, and pass a budget on it."""
        usage = merged([Usage(input_tokens=10, cost=0.1, currency="USD"), Usage(llm_calls=1)])

        assert (usage.input_tokens, usage.cost) == (None, None)


class TestLimits:
    def test_add_costs_goes_currency_by_currency(self) -> None:
        assert add_costs({"USD": 0.1}, {"USD": 0.2, "EUR": 1.0}) == {"USD": 0.3, "EUR": 1.0}

    def test_equal_to_a_limit_is_within_it(self) -> None:
        assert over_limit({"USD": 0.3}, 0.3) == {}
        assert over_limit({"USD": 0.31}, 0.3) == {"USD": 0.31}

    def test_amount_is_plain_digits(self) -> None:
        assert amount(0.000012) == "0.000012"
        assert amount(2) == "2"


class TestCheckBudget:
    @pytest.mark.parametrize(("used", "exceeded"), [(999, False), (1000, False), (1001, True)])
    def test_ac6_input_tokens_below_at_and_above_the_limit(self, used: int, exceeded: bool) -> None:
        check = check_budget(Budget(max_input_tokens=1000), [Usage(input_tokens=used)], "turn")

        assert bool(check.exceeded) is exceeded
        assert check.unverified == []

    @pytest.mark.parametrize(("cost", "exceeded"), [(0.29, False), (0.3, False), (0.31, True)])
    def test_ac6_cost_below_at_and_above_the_limit(self, cost: float, exceeded: bool) -> None:
        check = check_budget(Budget(max_cost=0.3), [_usd(cost)], "turn")

        assert bool(check.exceeded) is exceeded
        assert check.unverified == []

    def test_a_sum_equal_to_the_limit_is_within_it(self) -> None:
        check = check_budget(Budget(max_cost=0.3), [_usd(0.1), _usd(0.2)], "execution")

        assert (check.exceeded, check.unverified) == ([], [])

    def test_ac7_cached_input_counts_and_is_not_counted_twice(self) -> None:
        usage = Usage(input_tokens=1000, cached_input_tokens=800)

        over = check_budget(Budget(max_input_tokens=900), [usage], "turn")
        within = check_budget(Budget(max_input_tokens=1000), [usage], "turn")

        assert over.exceeded == [
            "the turn used 1000 input tokens (800 of them cached), over max_input_tokens 900"
        ]
        assert (within.exceeded, within.unverified) == ([], [])

    def test_ac9_no_usage_is_unverified(self) -> None:
        check = check_budget(Budget(max_cost=1), [None], "turn")

        assert check.exceeded == []
        assert "no usage" in check.unverified[0]

    def test_missing_the_field_a_limit_needs_is_unverified(self) -> None:
        tokens = check_budget(Budget(max_input_tokens=9), [Usage(cost=1.0)], "turn")
        cost = check_budget(Budget(max_cost=9), [Usage(input_tokens=1)], "turn")

        assert "input_tokens" in tokens.unverified[0]
        assert "cost" in cost.unverified[0]

    def test_known_parts_over_the_limit_are_enough_to_exceed_it(self) -> None:
        """Tokens only add up: what is missing cannot bring the sum back under."""
        check = check_budget(Budget(max_input_tokens=50), [Usage(input_tokens=100), None], "turn")

        assert check.exceeded
        assert check.unverified == []

    def test_known_parts_within_the_limit_prove_nothing(self) -> None:
        check = check_budget(Budget(max_input_tokens=500), [Usage(input_tokens=100), None], "turn")

        assert check.exceeded == []
        assert "1 of 2 turns without input_tokens" in check.unverified[0]

    def test_a_cost_without_a_currency_is_unverified(self) -> None:
        check = check_budget(Budget(max_cost=9), [Usage(cost=1.0)], "turn")

        assert "currency" in check.unverified[0]

    def test_currencies_within_the_limit_are_unverified(self) -> None:
        euro = Usage(cost=0.5, currency="EUR")

        check = check_budget(Budget(max_cost=0.9), [_usd(0.1), euro], "turn")

        assert check.exceeded == []
        assert "EUR, USD" in check.unverified[0]

    def test_a_currency_over_the_limit_exceeds_it(self) -> None:
        euro = Usage(cost=0.5, currency="EUR")

        check = check_budget(Budget(max_cost=0.2), [_usd(0.1), euro], "turn")

        assert check.exceeded == ["the turn cost 0.5 EUR, over max_cost 0.2"]

    def test_each_limit_is_checked(self) -> None:
        budget = Budget(max_input_tokens=1, max_cost=0.01)

        check = check_budget(budget, [_usd(0.5)], "turn")

        assert len(check.exceeded) == 2
