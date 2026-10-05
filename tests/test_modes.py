"""Game modes, and /gift."""

import asyncio
import socket
import sqlite3

import discord
from discord import app_commands

from mpr import bot as B
from mpr import db as DB
from mpr import economy
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
HOST = dict(user_id=99, name="Jon", host=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")
PARTY = "Mario, Luigi, Peach, Yoshi"
KART = ", ".join(f"Racer{i}" for i in range(1, 13))


def places(bot, path):
    cmd = bot.tree.get_command(path) if " " not in path else \
        bot.tree.get_command(path.split()[0]).get_command(path.split()[1])
    return [p.name for p in cmd.parameters if p.name in B.PLACES]


async def finish(bot, winners):
    """One /race result closes the night. Party needs coins; Kart has no props."""
    extra = {"coins": "tie"} if bot.mode == "party" else {}
    out = await run(bot, "race result", Interaction(**CREW), **dict(zip(B.PLACES, winners)), **extra)
    assert "is in the books" in out.text, out.text


# --- modes -------------------------------------------------------------------

def test_party_shows_four_places_and_kart_shows_twelve(fresh_db):
    async def go():
        bot = await build_bot()
        assert places(bot, "bet") == B.PLACES[:4]                      # party by default
        assert all(p.required for p in bot.tree.get_command("bet").parameters)

        await run(bot, "race create", Interaction(**CREW), mode="Mario Kart", week="MK",
                  runners=KART)
        assert places(bot, "bet") == B.PLACES
        assert places(bot, "race result") == B.PLACES
        await finish(bot, KART.split(", "))

        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="MP",
                  runners=PARTY)
        assert places(bot, "bet") == B.PLACES[:4]
        assert places(bot, "race result") == B.PLACES[:4]
        return bot.syncs

    syncs = asyncio.run(go())
    assert syncs == ["kart", "party"], "Discord is told only when the mode actually changes"


def test_same_mode_twice_doesnt_resync(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="A", runners=PARTY)
        await finish(bot, PARTY.split(", "))
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="B", runners=PARTY)
        return bot.syncs

    assert asyncio.run(go()) == []


def test_each_mode_enforces_its_field_size(fresh_db):
    async def go():
        bot = await build_bot()
        five = await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="X",
                         runners=PARTY + ", Wario")
        assert "Mario Party needs exactly 4 runners" in five.text and "gave 5" in five.text
        thirteen = await run(bot, "race create", Interaction(**CREW), mode="Mario Kart", week="X",
                             runners=KART + ", Racer13")
        assert "between 2 and 12" in thirteen.text
        eight = await run(bot, "race create", Interaction(**CREW), mode="Mario Kart", week="MK64",
                          runners=", ".join(f"R{i}" for i in range(8)), game="Mario Kart 64")
        assert "8 runners" in eight.text and "Mario Kart 64" in eight.text

    asyncio.run(go())


def test_kart_guess_and_payout_with_a_partial_field(fresh_db):
    """An eight-racer Kart game fills the first eight places and pays 8x."""
    db = fresh_db
    field = [f"R{i}" for i in range(8)]

    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Kart", week="MK64",
                  runners=", ".join(field))
        await run(bot, "race open", Interaction(**CREW))
        bet = await run(bot, "bet", Interaction(**ANA), amount=10, **dict(zip(B.PLACES, field)))
        assert "comes back as 80" in bet.text
        await finish(bot, field)

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100 - 10 + 80


def test_every_version_of_every_command_is_valid_for_discord(fresh_db):
    async def go():
        bot = await build_bot()
        for mode in DB.MODES:
            bot.install_mode(mode)
            for cmd in bot.tree.walk_commands():
                cmd.to_dict(bot.tree)
                assert len(cmd.description) <= 100
                if isinstance(cmd, app_commands.Command):
                    seen_optional = False
                    for prm in cmd.parameters:
                        assert not (prm.required and seen_optional), (mode, cmd.qualified_name)
                        seen_optional = seen_optional or not prm.required

    asyncio.run(go())


def test_a_restart_mid_kart_race_comes_back_in_kart_mode(fresh_db, monkeypatch):
    fresh_db.create_race(4242, "MK", "Mario Kart", KART.split(", "), mode="kart")
    monkeypatch.setattr(B, "GUILD_ID", 4242)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("MPR_OVERLAY_PORT", str(s.getsockname()[1]))

    async def go():
        bot = B.HorseRace()

        async def no_network():
            return None
        monkeypatch.setattr(bot, "sync_commands", no_network)
        await bot.setup_hook()
        for t in list(bot._background):
            t.cancel()
        await asyncio.gather(*bot._background, return_exceptions=True)
        return bot.mode, places(bot, "bet")

    mode, shown = asyncio.run(go())
    assert mode == "kart" and shown == B.PLACES


def test_a_stale_command_gets_told_to_reload(fresh_db):
    async def go():
        bot = await build_bot()
        inter = Interaction(**ANA)
        await B.on_error(inter, app_commands.CommandSignatureMismatch(bot.tree.get_command("bet")))
        return inter.text

    assert "Press Ctrl+R" in asyncio.run(go())


def test_kart_panel_and_scorebug_talk_in_laps(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Kart", week="MK", runners=KART)
        panel = await run(bot, "panel", Interaction(**CREW))
        return panel.sent[0]["content"]

    text = asyncio.run(go())
    assert "Lap 1 of 3" in text                   # counting starts at lap 1
    from fastapi.testclient import TestClient
    from mpr import overlay
    race = TestClient(overlay.app).get("/state.json").json()["race"]
    assert race["mode"] == "kart" and race["unit"] == "lap"


# --- /gift -----------------------------------------------------------------------

def test_gift_is_host_only_public_and_logged(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        member = discord.Object(id=20)
        member.display_name = "ana"
        crew = await run(bot, "gift", Interaction(**CREW), user=member, amount=50)
        assert "Only the server host can gift points" in crew.text

        out = await run(bot, "gift", Interaction(**HOST), user=member, amount=50,
                        reason="won the fan art contest")
        assert not out.sent[0]["ephemeral"]
        assert "Jon** gave **ana** 50 points: won the fan art contest. They now have 150." in out.text

        back = await run(bot, "gift", Interaction(**HOST), user=member, amount=-500, reason="oops")
        assert "took 150 points back" in back.text and "can't go below zero" in back.text

        none_left = await run(bot, "gift", Interaction(**HOST), user=member, amount=-5)
        assert "no points to take back" in none_left.text
        zero = await run(bot, "gift", Interaction(**HOST), user=member, amount=0)
        assert "other than zero" in zero.text

    asyncio.run(go())
    with db.connect() as conn:
        log = conn.execute("SELECT amount, reason, giver_id FROM gifts ORDER BY id").fetchall()
    assert [tuple(r) for r in log] == [(50, "won the fan art contest", 99), (-150, "oops", 99)]


def test_gifts_show_apart_from_winnings_and_reset_with_the_season(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        member = discord.Object(id=20)
        member.display_name = "ana"
        await run(bot, "gift", Interaction(**HOST), user=member, amount=75)
        status = await run(bot, "status", Interaction(**ANA))
        assert "Gifts from the crew this season: +75" in status.text
        assert "won back 0" in status.text                       # not counted as winnings
        w = db.wallet(20)
        assert w["balance"] == economy.STARTING_BALANCE - w["staked"] + w["returned"] + w["adjusted"]

        db.reset_economy(42, "Season 2")
        assert db.wallet(20)["adjusted"] == 0

    asyncio.run(go())
