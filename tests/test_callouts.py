"""Name callouts for big bets and all-ins."""

import asyncio
import time

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import economy, overlay
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB, KIM = dict(user_id=20, name="ana"), dict(user_id=30, name="rob"), dict(user_id=40, name="kim")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def order(*names):
    return dict(zip(B.PLACES, names))


async def open_night(bot):
    await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="W",
              runners=", ".join(FIELD))
    await run(bot, "race open", Interaction(**CREW))


def public(inter):
    return [m["content"] for m in inter.sent if not m["ephemeral"] and m["content"]]


def test_the_thresholds():
    assert economy.callout_kind(100, 100) == "allin"
    assert economy.callout_kind(100, 50) == "big"
    assert economy.callout_kind(100, 49) is None
    assert economy.callout_kind(15, 15) is None            # all in, but too small to hype
    assert economy.callout_kind(1000, 400) is None         # big number, small share


def test_all_in_and_big_bets_are_announced_publicly(fresh_db):
    async def go():
        bot = await build_bot()
        await open_night(bot)
        shove = await run(bot, "bet", Interaction(**ANA), amount=100, **order(*FIELD))
        half = await run(bot, "bet", Interaction(**ROB), amount=60, **order(*reversed(FIELD)))
        small = await run(bot, "bet", Interaction(**KIM), amount=10, **order(*FIELD))
        prop = await run(bot, "prop", Interaction(**ROB), market="Most coins at the end",
                         pick="Peach", amount=20)
        return shove, half, small, prop

    shove, half, small, prop = asyncio.run(go())
    assert public(shove) == ["ana just went ALL IN: 100 on Mario to win!"]
    assert shove.sent[0]["ephemeral"], "the bet slip itself stays private"
    assert public(half) == ["rob just put 60 on Yoshi to win!"]
    assert public(small) == []
    assert public(prop) == ["rob just put 20 on Peach for most coins at the end!"]   # half of his last 40


def test_bonus_bets_get_called_out_too(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        mid = db.open_bonus(db.active_race(42)["id"], "Who wins the next minigame?", FIELD, 60)
        view = B.bonus_view(mid)
        button = next(c for c in view.children if c.item.label == "Luigi")
        tap = Interaction(**ANA)
        await button.callback(tap)
        modal = tap.modals[0]
        modal.amount._value = "80"
        done = Interaction(**ANA)
        await modal.on_submit(done)
        return done

    done = asyncio.run(go())
    assert public(done) == ["ana just put 80 on Luigi on the bonus!"]


def test_a_cancelled_bet_takes_its_callout_off_the_feed(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=100, **order(*FIELD))
        rid = db.active_race(42)["id"]
        assert len(db.recent_callouts(rid)) == 1
        await run(bot, "cancel", Interaction(**ANA), bet="all")
        return db.recent_callouts(rid)

    assert asyncio.run(go()) == []


def test_the_scorebug_feed_only_carries_recent_callouts(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=100, **order(*FIELD))
        await run(bot, "bet", Interaction(**ROB), amount=100, **order(*FIELD))

    asyncio.run(go())
    with db.connect() as conn:
        conn.execute("UPDATE callouts SET created_at = ? WHERE who = 'rob'", (time.time() - 600,))
    feed = TestClient(overlay.app).get("/state.json").json()["callouts"]
    assert [(c["who"], c["amount"], c["what"], c["all_in"]) for c in feed] == \
        [("ana", 100, "Mario to win", True)]
    assert 0 <= feed[0]["age"] < 5
