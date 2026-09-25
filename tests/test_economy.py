"""The payout rules, checked against the spec's own examples and against
what a season of play actually does with them."""

import random
from itertools import permutations

import pytest

from mpr import economy as e
from mpr.economy import Ticket

FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def guess(bet_id, order, amount, user=1):
    return Ticket(bet_id, user, "slate", "order", list(order), amount)


# --- the spec's worked examples ----------------------------------------------

def test_spend_100_all_four_right_comes_out_with_400():
    assert e.grade_slates([guess(1, FIELD, 100)], FIELD) == {1: (4, 400)}


def test_spend_50_all_four_right_comes_out_with_200():
    assert e.grade_slates([guess(1, FIELD, 50)], FIELD) == {1: (4, 200)}


@pytest.mark.parametrize("order, right, back", [
    (["Mario", "Luigi", "Yoshi", "Peach"], 2, 200),   # two right: doubled
    (["Mario", "Peach", "Yoshi", "Luigi"], 1, 100),   # one right: stake back
    (["Luigi", "Mario", "Yoshi", "Peach"], 0, 0),     # none right: gone
])
def test_the_ladder_from_the_spec(order, right, back):
    assert e.grade_slates([guess(1, order, 100)], FIELD) == {1: (right, back)}


def test_three_right_cannot_happen_with_four_runners():
    counts = {e.positions_right(list(p), FIELD) for p in permutations(FIELD)}
    assert counts == {0, 1, 2, 4}
    assert [k for k, _, _ in e.ladder_table(4)] == [1, 2, 4]


def test_a_voided_order_refunds_every_guess():
    tickets = [guess(1, FIELD, 70), guess(2, reversed(FIELD), 30, user=2)]
    assert e.grade_slates(tickets, None) == {1: (0, 70), 2: (0, 30)}


# --- Mario Kart sized fields -------------------------------------------------

def test_twelve_racers_all_right_pays_twelve_times():
    field = [f"R{i}" for i in range(12)]
    assert e.grade_slates([guess(1, field, 100)], field) == {1: (12, 1200)}


def test_twelve_racers_partial_credit_counts_each_place():
    field = [f"R{i}" for i in range(12)]
    swapped = field[:]
    swapped[0], swapped[1] = swapped[1], swapped[0]     # ten right
    assert e.grade_slates([guess(1, swapped, 10)], field) == {1: (10, 100)}


# --- what the rule does to the economy ----------------------------------------

@pytest.mark.parametrize("n", [2, 4, 8, 12])
def test_a_random_guess_comes_out_exactly_even_at_any_field_size(n):
    """So placements neither print nor drain points on their own. Worth
    knowing if the leaderboard ever feels flat."""
    rtp = sum(e.chance_right(n, k) * e.slate_multiplier(k) for k in range(n + 1))
    assert rtp == pytest.approx(1.0)


def test_the_chances_on_the_payouts_card_add_up():
    for n in (4, 12):
        assert sum(e.chance_right(n, k) for k in range(n + 1)) == pytest.approx(1.0)
    assert e.chance_right(4, 0) == pytest.approx(0.375)      # 3 in 8 lose it all


def test_props_at_2x_return_half_on_average():
    assert e.PROP_MULTIPLIER * 0.25 == pytest.approx(0.5)


def test_all_in_every_week_busts_about_a_third_in_week_one():
    rng = random.Random(3)
    busted = 0
    for _ in range(20_000):
        order = rng.sample(FIELD, 4)
        right, back = e.grade_slates([guess(1, order, 100)], FIELD)[1]
        busted += back == 0
    assert busted / 20_000 == pytest.approx(0.375, abs=0.02)


# --- props, bonuses, tallies ---------------------------------------------------

def test_props_and_bonuses_pay_flat_and_void_refunds():
    tickets = [Ticket(1, 5, "prop", "coins", "Peach", 40),
               Ticket(2, 5, "prop", "coins", "Mario", 40)]
    assert e.grade_flat(tickets, "coins", "Peach", e.PROP_MULTIPLIER) == {1: 80, 2: 0}
    assert e.grade_flat(tickets, "coins", None, e.PROP_MULTIPLIER) == {1: 40, 2: 40}


def test_bonus_questions_default_to_fair_odds():
    assert e.bonus_multiplier(4) == 4 and e.bonus_multiplier(2) == 2


def test_tally_leader_voids_ties():
    assert e.tally_leader({"A": 3, "B": 1}) == "A"
    assert e.tally_leader({"A": 3, "B": 3}) is None
    assert e.tally_leader({"A": 0, "B": 0}) is None


# --- wallets and limits ------------------------------------------------------------

def test_everyone_starts_on_100_and_can_go_all_in():
    assert e.STARTING_BALANCE == 100
    assert e.check_wager(100, 0, 100) == (True, "")
    assert not e.check_wager(100, 0, 101)[0]
    assert not e.check_wager(100, 0, 0)[0]


def test_optional_ceiling_still_works_if_turned_on(monkeypatch):
    monkeypatch.setattr(e, "MAX_WAGER", 40)
    assert e.check_wager(100, 0, 40)[0]
    assert "up to 10 more" in e.check_wager(70, 30, 20)[1]


def test_rail_money_only_tops_up_the_broke():
    assert e.top_up(0) == e.RAIL_FLOOR
    assert e.top_up(e.RAIL_FLOOR) == 0
    assert e.top_up(5000) == 0
