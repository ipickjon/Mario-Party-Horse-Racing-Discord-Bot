"""Who can see and run what, and the /status command."""

import asyncio

import discord

from mpr import bot as B
from tests.harness import Interaction, build_bot, run

PLAYER_COMMANDS = {"bet", "prop", "payouts", "status", "wallet", "mybets", "board",
                   "leaderboard", "season", "ad", "help", "cancel", "form"}
CREW_COMMANDS = {"race", "show", "bonus", "panel", "tally", "railmoney", "feature", "review-ads"}
HOST_COMMANDS = {"reset", "backup", "gift", "overlay-links"}

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")


def top_level():
    async def go():
        bot = await build_bot()
        return {c.name: c.default_permissions for c in bot.tree.get_commands()}
    return asyncio.run(go())


def test_every_command_is_sorted_into_players_crew_or_host(fresh_db):
    """A new command has to be put in one of these on purpose."""
    assert set(top_level()) == PLAYER_COMMANDS | CREW_COMMANDS | HOST_COMMANDS


def test_players_see_only_player_commands(fresh_db):
    perms = top_level()
    for name in PLAYER_COMMANDS:
        assert perms[name] is None, f"/{name} should be visible to everyone"
    for name in CREW_COMMANDS:
        assert perms[name] is not None and perms[name].manage_events, \
            f"/{name} should be hidden from players"
    for name in HOST_COMMANDS:
        assert perms[name] is not None and perms[name].manage_guild, f"/{name} is host only"


def test_hidden_isnt_the_only_lock(fresh_db):
    """Server admins can override Discord's visibility, so every crew command
    still refuses a non-crew member if they reach it anyway."""
    async def go():
        bot = await build_bot()
        for path, kwargs in [
            ("race call", dict(market="Most coins at the end", winner="Mario")),
            ("bonus call", dict(market="1", winner="Mario")),
            ("race result", dict(first="Mario", second="Luigi", third="Peach", fourth="Yoshi",
                                 coins="Mario")),
            ("race open", {}), ("show next", {}), ("panel", {}), ("railmoney", dict(week="W")),
            ("review-ads", {}),
        ]:
            out = await run(bot, path, Interaction(**ANA), **kwargs)
            assert "for the crew" in out.text, path

    asyncio.run(go())


def test_crew_visibility_permission_is_a_harmless_one():
    assert B.CREW_VISIBLE == discord.Permissions(manage_events=True)


# --- /status ---------------------------------------------------------------------

def order(*names):
    return dict(zip(B.PLACES, names))


def test_status_for_a_brand_new_player(fresh_db):
    async def go():
        bot = await build_bot()
        out = await run(bot, "status", Interaction(**ANA))
        assert out.sent[0]["ephemeral"]
        return out.text

    text = asyncio.run(go())
    assert "100 points" in text and "1st of 1" in text


def test_status_follows_a_night_from_riding_to_results(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="Week 2",
                  runners="Mario, Luigi, Peach, Yoshi")
        await run(bot, "race open", Interaction(**CREW))
        empty = await run(bot, "status", Interaction(**ANA))
        assert "No bets. `/bet`" in empty.text and "betting open" in empty.text

        await run(bot, "bet", Interaction(**ANA), amount=40, **order("Mario", "Luigi", "Peach", "Yoshi"))
        await run(bot, "prop", Interaction(**ANA), market="Most coins at the end", pick="Luigi", amount=10)
        await run(bot, "bet", Interaction(**ROB), amount=100, **order("Yoshi", "Peach", "Luigi", "Mario"))
        riding = await run(bot, "status", Interaction(**ANA))
        assert "Mario > Luigi > Peach > Yoshi" in riding.text and "riding" in riding.text
        assert "50 points" in riding.text and "1st of" in riding.text     # rob is on 0

        await run(bot, "race result", Interaction(**CREW), **order("Mario", "Luigi", "Peach", "Yoshi"),
                  coins="Peach")
        done = await run(bot, "status", Interaction(**ANA))
        assert "won 160" in done.text and "lost" in done.text
        assert "bet 50, won back 160 (+110)" in done.text

        broke = await run(bot, "status", Interaction(**ROB))
        assert "0 points" in broke.text and "out of points" in broke.text
        assert "2nd of 2" in broke.text      # the crew never bet, so they have no wallet

    asyncio.run(go())


def test_status_mentions_your_ads(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**ANA), headline="Ana's pitch")
        return (await run(bot, "status", Interaction(**ANA))).text

    assert "Your ads: 1 waiting for review" in asyncio.run(go())


def test_tied_balances_share_a_place(fresh_db):
    db = fresh_db
    for uid in (1, 2, 3):
        db.wallet(uid, f"p{uid}")
    with db.connect() as conn:
        conn.execute("UPDATE wallets SET balance = 500 WHERE user_id = 3")
    assert db.rank_of(3) == (1, 3)
    assert db.rank_of(1) == (2, 3) and db.rank_of(2) == (2, 3)


# --- /help and /overlay-links ----------------------------------------------------

def test_help_shows_the_checklist_only_to_crew(fresh_db):
    async def go():
        bot = await build_bot()
        viewer = await run(bot, "help", Interaction(**ANA))
        crew = await run(bot, "help", Interaction(**CREW))
        return viewer, crew

    viewer, crew = asyncio.run(go())
    assert "/bet" in viewer.text and "show night" not in viewer.text
    assert viewer.sent[0]["embed"].fields == []
    checklist = crew.sent[0]["embed"].fields[0].value
    for step in ("/race create", "/show start", "/show next", "/race result"):
        assert step in checklist


def test_overlay_links_are_host_only_private_and_complete(fresh_db, monkeypatch):
    from mpr import overlay
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "mpr.up.railway.app")
    monkeypatch.setattr(overlay, "OVERLAY_KEY", "k3y")

    async def go():
        bot = await build_bot()
        crew = await run(bot, "overlay-links", Interaction(**CREW))
        host = await run(bot, "overlay-links", Interaction(user_id=99, host=True))
        return crew, host

    crew, host = asyncio.run(go())
    assert "Only the server host" in crew.text
    assert host.sent[0]["ephemeral"]
    for page in ("bug", "bonus", "tote", "casters", "ads", "standings"):
        assert f"https://mpr.up.railway.app/{page}?key=k3y" in host.text
