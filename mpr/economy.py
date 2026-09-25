"""Payout math for Mario Party Horse Racing.

Placements: each player guesses the full finishing order and puts one stake
on it. They get back the stake times the number of positions they got right,
per the spec:

    spend 100, get all 4 right  ->  400
    spend  50, get all 4 right  ->  200
    get 2 right                 ->  stake doubled
    get 1 right                 ->  stake back
    get none right              ->  stake lost

Three right can't happen with four runners: if three are right, the fourth
is forced. The rule works unchanged for any field size, so a 12-racer Mario
Kart guess tops out at 12x.

A random guess comes out exactly even on average at any field size, because
a random order matches the result in one position on average. So the
placement market neither prints nor drains points by itself; the leaderboard
moves on who reads the race better and who sizes their bets well.

Props (most minigames, most coins, most ? tiles) pay 2x. On a one-in-four
pick that returns half the stake on average: props are where points leak
out, which is fine if they're meant as the fun, risky side bets.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, factorial

# Positions right -> what the stake is multiplied by. From the spec. Any count
# not listed pays its own number, so bigger fields need no extra table.
PLACEMENT_LADDER = {1: 1, 2: 2, 4: 4}

PROP_MULTIPLIER = 2

STARTING_BALANCE = 100

# Opt-in via /railmoney: anyone below this is topped back up to it. Pays
# nobody who is already above it, so the leaderboard still ranks results.
RAIL_FLOOR = 100

# Broadcasts per season, used for the season progress readout.
SEASON_LENGTH = 10

# Bet limits. The spec lets a player put their whole stack on one guess, so
# there's no ceiling by default. Set MAX_WAGER to a number, or the fraction
# below 1.0, to stop anyone betting everything at once.
MAX_WAGER: int | None = None
MAX_WAGER_FRACTION = 1.0
MIN_WAGER = 1

MIN_RUNNERS, MAX_RUNNERS = 2, 12


def slate_multiplier(correct: int) -> int:
    if correct <= 0:
        return 0
    return PLACEMENT_LADDER.get(correct, correct)


def bonus_multiplier(options: int) -> int:
    """Default price for a mid-game bonus: fair odds. A pick from four pays
    4x, a yes/no 2x. Override per question if you want them juicier."""
    return max(2, options)


def top_up(balance: int) -> int:
    return max(0, RAIL_FLOOR - balance)


def check_wager(balance: int, already_on_market: int, amount: int) -> tuple[bool, str]:
    """Check one more wager on a market someone may already have money on."""
    if amount < MIN_WAGER:
        return False, f"Minimum bet is {MIN_WAGER}."
    if amount > balance:
        return False, f"You have {balance:,} points."
    stack = balance + already_on_market
    ceiling = int(stack * MAX_WAGER_FRACTION)
    if MAX_WAGER is not None:
        ceiling = min(ceiling, MAX_WAGER)
    ceiling = max(ceiling, MIN_WAGER)
    room = ceiling - already_on_market
    if amount > room:
        if room < MIN_WAGER:
            return False, f"You're at the {ceiling:,} limit on this market."
        return False, f"You can add up to {room:,} more on this market."
    return True, ""


@dataclass
class Ticket:
    """One wager, ready to grade. For a placement guess, selection is the
    guessed order; for everything else it's a single pick."""

    bet_id: int
    user_id: int
    kind: str          # 'slate', 'prop' or 'bonus'
    key: str
    selection: object
    amount: int


def positions_right(guess: list[str], finish: list[str]) -> int:
    return sum(1 for g, f in zip(guess, finish) if g == f)


def grade_slates(tickets: list[Ticket], finish: list[str] | None) -> dict[int, tuple[int, int]]:
    """Grade placement guesses against the final order.

    Returns bet id -> (positions right, points back). finish=None voids the
    placements and refunds every stake.
    """
    out: dict[int, tuple[int, int]] = {}
    for t in tickets:
        if t.kind != "slate":
            continue
        if finish is None:
            out[t.bet_id] = (0, t.amount)
            continue
        right = positions_right(t.selection, finish)
        out[t.bet_id] = (right, t.amount * slate_multiplier(right))
    return out


def grade_flat(tickets: list[Ticket], key: str, winner: str | None,
               multiplier: int) -> dict[int, int]:
    """Grade one single-pick market (a prop or a bonus). None voids it."""
    payouts: dict[int, int] = {}
    for t in tickets:
        if t.key != key:
            continue
        if winner is None:
            payouts[t.bet_id] = t.amount
        elif t.selection == winner:
            payouts[t.bet_id] = t.amount * multiplier
        else:
            payouts[t.bet_id] = 0
    return payouts


def tally_leader(counts: dict[str, int]) -> str | None:
    """The outright leader of a live tally, or None on a tie or no data.
    Ties void the prop: refunding everyone beats paying the wrong people."""
    if not counts:
        return None
    top = max(counts.values())
    if top <= 0:
        return None
    leaders = [name for name, n in counts.items() if n == top]
    return leaders[0] if len(leaders) == 1 else None


def _derangements(n: int) -> int:
    a, b = 1, 0
    if n == 0:
        return 1
    for i in range(2, n + 1):
        a, b = b, (i - 1) * (a + b)
    return b


def chance_right(n: int, k: int) -> float:
    """Chance a random full guess of n finishers gets exactly k right."""
    return comb(n, k) * _derangements(n - k) / factorial(n)


def ladder_table(n: int = 4) -> list[tuple[int, int, float]]:
    """(positions right, multiplier, how often a random guess does it), for
    the tiers that can actually happen with n runners."""
    return [(k, slate_multiplier(k), chance_right(n, k))
            for k in range(1, n + 1) if chance_right(n, k) > 0]
