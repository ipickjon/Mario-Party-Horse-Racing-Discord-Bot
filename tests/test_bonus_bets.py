"""Bonus bets: types, answer limits, Draw, crew buttons, free comeback picks,
bets left open until betting locks, and the minigame button on the panel."""

import asyncio
import time
import types

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import economy, overlay
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def buttons(view):
    return {getattr(c, "item", c).label: c for c in view.children}


async def press(item, who=CREW):
    inter = Interaction(**who)
    if await item.interaction_check(inter):
        await item.callback(inter)
    return inter


async def back(button, who, amount):
    """Tap an answer and submit a stake through the modal."""
    tapped = await press(button, who)
    if not tapped.modals:
        return tapped
    modal = tapped.modals[0]
    modal.amount._value = str(amount)
    done = Interaction(**who)
    await modal.on_submit(done)
    return done


async def night(bot):
    await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="W",
              runners=", ".join(FIELD))
    await run(bot, "race open", Interaction(**CREW))


def test_yes_or_no_backs_one_side_and_characters_back_two(fresh_db):
    async def go():
        bot = await build_bot()
        await night(bot)
        yn = await run(bot, "bonus bet", Interaction(**CREW), question="Will Luigi turn it around?",
                       type="Yes or No", timer="90 seconds")
        assert "Pays **2x**" in yn.text and "Back one answer" in yn.text
        bb = buttons(yn.sent[0]["view"])
        assert {"Yes", "No"} <= set(bb)
        await back(bb["Yes"], ANA, 10)
        second_side = await back(bb["No"], ANA, 10)
        assert "One side only" in second_side.text
        more_yes = await back(bb["Yes"], ANA, 10)               # same side again is fine
        assert "10 on **Yes**" in more_yes.text

        ch = await run(bot, "bonus bet", Interaction(**CREW), question="Who's last after turn 10?",
                       type="Pick a character", timer="90 seconds")
        cb = buttons(ch.sent[0]["view"])
        await back(cb["Mario"], ROB, 10)
        await back(cb["Luigi"], ROB, 10)
        third = await back(cb["Peach"], ROB, 10)
        assert "at most 2 answers" in third.text

    asyncio.run(go())


def test_minigame_bets_have_a_draw_at_8x_and_two_winners(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        mg = await run(bot, "bonus bet", Interaction(**CREW), question="Who wins this minigame?",
                       type="Minigame winner")
        bb = buttons(mg.sent[0]["view"])
        assert "Draw" in bb and "Draw** (8x)" in mg.text
        await back(bb["Draw"], ANA, 10)
        await back(bb["Mario"], ROB, 10)
        await back(bb["Luigi"], ROB, 10)
        mid = db.bonus_markets(db.active_race(42)["id"])[0]["id"]
        assert db.market_by_id(mid)["closes_at"] - time.time() <= 61     # 60-second default
        return mid

    mid = asyncio.run(go())
    out = db.call_market_id(mid, ["Mario", "Luigi"])                     # a 2 v 2
    assert out["winners"] == 2 and "Mario and Luigi" in out["winner"]
    assert db.wallet(30)["balance"] == 100 - 20 + 40 + 40
    db.wallet(20)
    assert db.wallet(20)["balance"] == 90                                 # draw didn't happen


def test_a_draw_pays_eight_times(fresh_db):
    db = fresh_db
    rid = db.create_race(42, "W", "MP", FIELD)
    mid = db.open_bonus(rid, "Who wins?", FIELD + ["Draw"], 60, None, "minigame", 2)
    db.wallet(20, "ana")
    db.place_bet(rid, mid, 20, "Draw", 10)
    db.call_market_id(mid, "Draw")
    assert db.wallet(20)["balance"] == 100 - 10 + 80


def test_crew_buttons_close_pay_and_delete(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        post = await run(bot, "bonus bet", Interaction(**CREW), question="Delete me",
                         type="Pick a character", timer="3 minutes")
        bb = buttons(post.sent[0]["view"])
        await back(bb["Peach"], ANA, 30)
        assert db.wallet(20)["balance"] == 70
        assert "Crew only." in (await press(bb["Crew: Delete"], ANA)).text
        gone = Interaction(**CREW)
        gone.message.deleted = False

        async def delete():
            gone.message.deleted = True
        gone.message.delete = delete
        await bb["Crew: Delete"].callback(gone)
        assert "1 bets refunded" in gone.text and gone.message.deleted

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100


def test_bets_left_open_until_betting_locks(fresh_db):
    """The 'who's first to the bank?' bets set up at the start: open while
    betting is, closed when the race starts, refunded if it never happens."""
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        a = await run(bot, "bonus bet", Interaction(**CREW), question="First to land on the bank?",
                      type="Pick a character", timer="Until betting locks for the race")
        b = await run(bot, "bonus bet", Interaction(**CREW), question="First to get an item?",
                      type="Pick a character", timer="Until betting locks for the race")
        assert "open until betting locks" in a.text
        await back(buttons(a.sent[0]["view"])["Mario"], ANA, 20)
        await back(buttons(b.sent[0]["view"])["Yoshi"], ANA, 20)
        rid = db.active_race(42)["id"]
        bank, item = [m["id"] for m in sorted(db.bonus_markets(rid), key=lambda m: m["id"])]

        state = TestClient(overlay.app).get("/state.json").json()["bonus"]
        assert state["until_lock"] and state["state"] == "open"

        await run(bot, "race lock", Interaction(**CREW))
        assert not db.bonus_taking_bets(db.market_by_id(bank))
        assert TestClient(overlay.app).get("/state.json").json()["bonus"] is None  # off screen
        db.call_market_id(item, "Yoshi")                      # someone got an item
        await run(bot, "race result", Interaction(**CREW), **dict(zip(B.PLACES, FIELD)), coins="tie")
        return bank

    bank = asyncio.run(go())
    assert db.market_by_id(bank)["result"] == "VOID"          # nobody landed on the bank
    assert db.wallet(20)["balance"] == 100 - 40 + 20 + 80     # bank refunded, item paid 4x


def test_the_comeback_free_pick(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        db.wallet(20, "ana")
        with db.connect() as conn:
            conn.execute("UPDATE wallets SET balance = 0 WHERE user_id = 20")
        mg = await run(bot, "bonus bet", Interaction(**CREW), question="Who wins this minigame?",
                       type="Minigame winner")
        bb = buttons(mg.sent[0]["view"])
        free = await press(bb["Peach"], ANA)
        assert not free.modals and "this one's free" in free.text
        again = await press(bb["Mario"], ANA)
        assert "already got your free pick" in again.text

        ch = await run(bot, "bonus bet", Interaction(**CREW), question="Not a minigame",
                       type="Pick a character")
        nope = await press(buttons(ch.sent[0]["view"])["Peach"], ANA)
        assert "Minigame bets give you a free pick" in nope.text
        return db.bonus_markets(db.active_race(42)["id"])[-1]["id"]

    mid = asyncio.run(go())
    db.call_market_id(mid, "Peach")
    assert db.wallet(20)["balance"] == economy.COMEBACK_PRIZE


def test_the_panel_opens_a_minigame_bet_and_settles_first_star(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night(bot)
        await run(bot, "sidebet", Interaction(**ANA), market="First to get a star", pick="Peach", amount=10)
        await run(bot, "race lock", Interaction(**CREW))
        panel = await run(bot, "panel", Interaction(**CREW))
        pb = buttons(panel.sent[0]["view"])
        assert {"★ Mario", "🎮 Minigame bet"} <= set(pb)

        bet = await press(pb["🎮 Minigame bet"])
        assert "Who wins this minigame?" in bet.text and "Draw" in bet.text
        star = await press(pb["★ Peach"])
        assert "First star: Peach" in star.text and "1 of 1 side bets cashed" in star.text
        assert "★ Peach" not in {getattr(c, "item", c).label for c in star.edits[-1]["view"].children}

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100 - 10 + 20


def test_first_star_on_race_result_or_refunded(fresh_db):
    db = fresh_db

    async def go(first_star):
        bot = await build_bot()
        await night(bot)
        await run(bot, "sidebet", Interaction(**ANA), market="First to get a star", pick="Luigi", amount=10)
        extra = {"first_star": first_star} if first_star else {}
        await run(bot, "race result", Interaction(**CREW), **dict(zip(B.PLACES, FIELD)),
                  coins="tie", **extra)

    asyncio.run(go("Luigi"))
    assert db.wallet(20)["balance"] == 100 + 10          # paid 2x
    asyncio.run(go(None))
    assert db.wallet(20)["balance"] == 110               # nobody got a star: refunded


def test_free_picks_are_only_for_players_at_zero(fresh_db):
    """The button only offers a free pick at zero, but the rule is enforced
    where the bet is stored too, in case anything else ever calls it."""
    db = fresh_db
    rid = db.create_race(42, "W", "MP", FIELD)
    db.set_race_status(rid, "open")
    mid = db.open_bonus(rid, "Who wins?", FIELD + ["Draw"], 60, None, "minigame", 2)
    ok, why = db.place_comeback(mid, 20, "ana", "Mario")          # ana has 100
    assert not ok and "still have points" in why
