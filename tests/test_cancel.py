"""/cancel: take a bet back while betting's open, never after."""

import asyncio
import time

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import economy, overlay
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def order(*names):
    return dict(zip(B.PLACES, names))


async def open_night(bot):
    await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="W",
              runners=", ".join(FIELD))
    await run(bot, "race open", Interaction(**CREW))


def ledger_balances(db, uid):
    w = db.wallet(uid)
    return w["balance"] == economy.STARTING_BALANCE - w["staked"] + w["returned"] + w["adjusted"]


def test_cancel_refunds_in_full_and_leaves_no_trace(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        placed = await run(bot, "bet", Interaction(**ANA), amount=40, **order(*FIELD))
        assert "`/cancel` it before betting locks" in placed.text
        bet_id = db.cancellable_bets(db.active_race(42)["id"], 20)[0]["id"]

        out = await run(bot, "cancel", Interaction(**ANA), bet=str(bet_id))
        assert "Took back 40 on Mario > Luigi > Peach > Yoshi (Finishing order)" in out.text
        assert "you have 100" in out.text and out.sent[0]["ephemeral"]

        status = await run(bot, "status", Interaction(**ANA))
        assert "bet 0, won back 0" in status.text and "Mario > Luigi" not in status.text

    asyncio.run(go())
    w = db.wallet(20)
    assert w["balance"] == 100 and w["staked"] == 0 and ledger_balances(db, 20)
    first_place = TestClient(overlay.app).get("/state.json").json()["markets"][0]
    assert first_place["staked"] == 0, "the tote board forgets it too"


def test_change_a_bet_by_cancelling_and_betting_again(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=100, **order(*FIELD))
        rid = db.active_race(42)["id"]
        await run(bot, "cancel", Interaction(**ANA), bet=str(db.cancellable_bets(rid, 20)[0]["id"]))
        again = await run(bot, "bet", Interaction(**ANA), amount=100, **order("Yoshi", "Peach", "Luigi", "Mario"))
        assert "100 on **Yoshi > Peach > Luigi > Mario**" in again.text
        return [b["pick_text"] for b in db.cancellable_bets(rid, 20)]

    assert asyncio.run(go()) == ["Yoshi > Peach > Luigi > Mario"]


def test_bets_are_final_once_betting_locks(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=40, **order(*FIELD))
        rid = db.active_race(42)["id"]
        bet_id = db.cancellable_bets(rid, 20)[0]["id"]
        await run(bot, "race lock", Interaction(**CREW))
        out = await run(bot, "cancel", Interaction(**ANA), bet=str(bet_id))
        assert "Betting's locked on that one" in out.text
        assert await B.cancel_options(Interaction(**ANA), "") == []   # nothing offered

        await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="tie")
        late = await run(bot, "cancel", Interaction(**ANA), bet=str(bet_id))
        assert "No race is set up" in late.text or "already been settled" in late.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100 - 40 + 160                  # the bet stood


def test_you_can_only_cancel_your_own(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=40, **order(*FIELD))
        ana_bet = db.cancellable_bets(db.active_race(42)["id"], 20)[0]["id"]
        sneaky = await run(bot, "cancel", Interaction(**ROB), bet=str(ana_bet))
        assert "isn't one of your bets" in sneaky.text
        assert await B.cancel_options(Interaction(**ROB), "") == []

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 60


def test_cancel_all_and_the_picker(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "bet", Interaction(**ANA), amount=30, **order(*FIELD))
        await run(bot, "prop", Interaction(**ANA), market="Most coins at the end", pick="Peach", amount=20)
        picker = [c.name for c in await B.cancel_options(Interaction(**ANA), "")]
        assert picker[0] == "All 2 of my open bets"
        assert any("Peach (Most coins at the end)" in name for name in picker)
        out = await run(bot, "cancel", Interaction(**ANA), bet="all")
        assert "50 points back, you have 100" in out.text
        none_left = await run(bot, "cancel", Interaction(**ANA), bet="all")
        assert "don't have any bets you can take back" in none_left.text

    asyncio.run(go())


def test_bonus_bets_can_be_cancelled_until_the_timer_runs_out(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await open_night(bot)
        await run(bot, "race lock", Interaction(**CREW))
        rid = db.active_race(42)["id"]
        mid = db.open_bonus(rid, "Who wins the next minigame?", FIELD, 60)
        B.submit_bonus_stake(20, "ana", mid, 0, "25")
        B.submit_bonus_stake(20, "ana", mid, 1, "10")
        first, second = [b["id"] for b in db.cancellable_bets(rid, 20)]
        ok = await run(bot, "cancel", Interaction(**ANA), bet=str(first))
        assert "Took back 25 on Mario (Who wins the next minigame?)" in ok.text

        with db.connect() as conn:
            conn.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (time.time() - 1, mid))
        closed = await run(bot, "cancel", Interaction(**ANA), bet=str(second))
        assert "Betting's locked" in closed.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100 - 10
