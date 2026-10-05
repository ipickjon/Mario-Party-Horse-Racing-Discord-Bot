"""Undo, who-bet-what, icons, leaderboard place, turns from 1, 1,000 points,
and the one-time pin for the Discord ad on servers seeded earlier."""

import asyncio
import types

import pytest

from mpr import bot as B
from mpr import economy
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def order(*names):
    return dict(zip(B.PLACES, names))


async def night(bot):
    await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="W",
              runners=", ".join(FIELD))
    await run(bot, "race open", Interaction(**CREW))


def test_undo_button_on_the_bet_slip(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        slip = await run(bot, "bet", Interaction(**ANA), amount=40, **order(*FIELD))
        undo = next(c for c in slip.sent[0]["view"].children if c.item.label == "Undo this bet")
        stranger = Interaction(**ROB)
        await undo.callback(stranger)
        assert "isn't one of your bets" in stranger.text
        mine = Interaction(**ANA)
        await undo.callback(mine)
        assert "Taken back: 40" in mine.edits[-1]["content"] and mine.edits[-1]["view"] is None
        again = Interaction(**ANA)
        await undo.callback(again)
        assert "isn't one of your bets" in again.text          # already gone

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100


def test_everyone_sees_who_bet_what(fresh_db):
    async def go():
        bot = await build_bot()
        await night(bot)
        bet = await run(bot, "bet", Interaction(**ANA), amount=10, **order(*FIELD))
        side = await run(bot, "sidebet", Interaction(**ROB), market="Most coins at the end",
                         pick="Peach", amount=10)
        board = await run(bot, "bets", Interaction(**ROB))
        return bet, side, board

    bet, side, board = asyncio.run(go())
    public = [m["content"] for m in bet.sent if not m["ephemeral"]]
    assert public == ["**ana** bet 10: 🟥 Mario › 🟩 Luigi › 🟧 Peach › 🟦 Yoshi"]
    assert [m["content"] for m in side.sent if not m["ephemeral"]] == \
        ["**rob** bet 10 on 🟧 Peach, most coins at the end."]
    assert "**ana** 10: 🟥 Mario › 🟩 Luigi" in board.text
    assert "**rob** 10: 🟧 Peach" in board.text and board.sent[0]["ephemeral"]


def test_server_emoji_named_after_a_runner_is_used_as_its_icon():
    guild = types.SimpleNamespace(emojis=[types.SimpleNamespace(name="Mario", __str__=None)])

    class Emoji:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return f"<:{self.name}:123>"
    guild.emojis = [Emoji("mario"), Emoji("dry_bones")]
    assert B.runner_icon(guild, "Mario", 1) == "<:mario:123>"
    assert B.runner_icon(guild, "Dry Bones", 3) == "<:dry_bones:123>"
    assert B.runner_icon(guild, "Peach", 3) == "🟧"                  # no emoji: colour square
    assert B.runner_icon(None, "Racer9", 9) == "▫️"


def test_leaderboard_shows_your_place_when_youre_outside_the_top_ten(fresh_db):
    db = fresh_db
    for uid in range(100, 112):
        db.wallet(uid, f"p{uid}")
        db.gift(uid, f"p{uid}", 50, 1)
    db.wallet(20, "ana")

    async def go():
        bot = await build_bot()
        out = await run(bot, "leaderboard", Interaction(**ANA))
        inside = await run(bot, "leaderboard", Interaction(user_id=100, name="p100"))
        return out.text, inside.text

    outside, inside = asyncio.run(go())
    assert outside.count("**p1") == 10                                   # top ten only
    assert "You: 13th of 13, 100" in outside
    assert "You:" not in inside


def test_turns_count_from_one(fresh_db):
    db = fresh_db
    rid = db.create_race(42, "W", "MP", FIELD)
    race = db.race(rid)
    assert race["turn"] == 1 and race["total_turns"] == 20
    assert db.bump_turn(rid, -1) == 1                                   # can't go below 1
    assert db.bump_turn(rid, 1) == 2
    db.undo_last(rid)
    assert db.race(rid)["turn"] == 1
    kart = db.create_race(42, "K", "MK", [f"R{i}" for i in range(8)], mode="kart")
    assert db.race(kart)["turn"] == 1 and db.race(kart)["total_turns"] == 3


@pytest.mark.real_economy
def test_everyone_starts_with_a_thousand(fresh_db):
    assert economy.STARTING_BALANCE == 1000 and economy.RAIL_FLOOR == 1000
    assert fresh_db.wallet(20, "ana")["balance"] == 1000


def test_live_servers_get_the_discord_ad_pinned_once(fresh_db):
    """A server seeded before pinning existed has its Discord ad pinned on
    the next start, exactly once, so un-pinning it later sticks."""
    db = fresh_db
    with db.connect() as conn:
        conn.execute("UPDATE ads SET pinned = 0")
        conn.execute("DELETE FROM meta WHERE key = 'ads_pinned'")
    db.init()
    pinned = [a["headline"] for a in db.ads() if a["pinned"]]
    assert pinned == ["Join the Discord"]
    db.set_pinned(db.ads()[0]["id"], False)
    db.init()
    assert not any(a["pinned"] for a in db.ads())
