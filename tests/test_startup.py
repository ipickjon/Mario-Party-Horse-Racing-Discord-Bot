"""The bot's startup path, short of logging in to Discord."""

import asyncio
import socket
import urllib.request

import discord

from mpr import bot as B
from mpr import overlay


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_startup_registers_buttons_syncs_to_guild_and_serves_overlay(fresh_db, monkeypatch):
    port = free_port()
    monkeypatch.setenv("MPR_OVERLAY_PORT", str(port))
    monkeypatch.setattr(B, "GUILD_ID", 4242)
    synced = []

    async def go():
        bot = B.HorseRace()

        async def fake_sync(*, guild=None):
            synced.append(guild.id if guild else None)
            return []
        monkeypatch.setattr(bot.tree, "sync", fake_sync)

        await bot.setup_hook()
        # buttons from before a restart must still route to a handler
        stored = bot._connection._view_store._dynamic_items
        assert any(cls is B.TallyButton for cls in stored.values())
        assert any(cls is B.BonusButton for cls in stored.values())
        assert {c.qualified_name for c in bot.tree.get_commands(guild=discord.Object(4242))} \
            >= {"bet", "race", "show", "bonus", "panel"}

        body = None
        for _ in range(50):                       # overlay comes up in the same loop
            await asyncio.sleep(0.1)
            try:
                body = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/state.json", timeout=1).read())
                break
            except OSError:
                continue
        for task in list(bot._background):
            task.cancel()
        await asyncio.gather(*bot._background, return_exceptions=True)
        return body

    body = asyncio.run(go())
    assert synced == [4242], "commands should sync to the guild, not globally"
    assert body is not None and b'"race"' in body


def test_restart_closes_bonus_markets_that_ran_out_while_offline(fresh_db, monkeypatch):
    db = fresh_db
    rid = db.create_race(1, "W", "MP", ["A", "B", "C", "D"])
    mid = db.open_bonus(rid, "Q", ["A", "B"], 60)
    with db.connect() as conn:
        conn.execute("UPDATE markets SET closes_at = 1 WHERE id = ?", (mid,))
    monkeypatch.setenv("MPR_OVERLAY_PORT", str(free_port()))

    async def go():
        bot = B.HorseRace()

        async def fake_sync(*, guild=None):
            return []
        monkeypatch.setattr(bot.tree, "sync", fake_sync)
        await bot.setup_hook()
        for task in list(bot._background):
            task.cancel()
        await asyncio.gather(*bot._background, return_exceptions=True)

    asyncio.run(go())
    assert db.market_by_id(mid)["status"] == "locked"
