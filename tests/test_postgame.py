"""/race result as the whole post-game, and automatic snapshots."""

import asyncio
import time

from mpr import bot as B
from mpr import db as DB
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA = dict(user_id=20, name="ana")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def order(*names):
    return dict(zip(B.PLACES, names))


async def night_with_bets(bot):
    await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="Week 9",
              runners=", ".join(FIELD))
    await run(bot, "race open", Interaction(**CREW))
    await run(bot, "bet", Interaction(**ANA), amount=30, **order(*FIELD))
    await run(bot, "prop", Interaction(**ANA), market="Most coins at the end", pick="Luigi", amount=10)
    return DB.active_race(42)["id"]


def paid_anything(db, rid) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM bets WHERE race_id = ? AND settled = 1",
                            (rid,)).fetchone()[0] > 0


def test_every_refusal_leaves_the_night_untouched(fresh_db):
    """The crew will fix a mistake and run it again mid-show, so a refusal
    must not have half-paid anything."""
    db = fresh_db

    async def go():
        bot = await build_bot()
        rid = await night_with_bets(bot)
        mid = db.open_bonus(rid, "Who wins the next one?", FIELD, 60)

        blocked = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Luigi")
        assert "Settle the bonus questions first" in blocked.text and "Who wins the next one?" in blocked.text

        with db.connect() as conn:
            conn.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (time.time() - 1, mid))
        db.call_market_id(mid, None)

        dup = await run(bot, "race result", Interaction(**CREW),
                        **order("Mario", "Mario", "Peach", "Yoshi"), coins="Luigi")
        assert "only finish in one place" in dup.text
        stranger = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Bowser")
        assert "Bowser isn't in this race" in stranger.text
        forgot = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="")
        assert "Fill in coins" in forgot.text

        assert not paid_anything(db, rid) and db.race(rid)["status"] == "open"
        assert db.wallet(20)["balance"] == 60

        ok = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Luigi")
        assert "Week 9 is in the books" in ok.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 60 + 30 * 4 + 10 * 2


def test_coins_tie_refunds_the_prop(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await night_with_bets(bot)
        out = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="TIE")
        assert "Most coins at the end:** tie, bets refunded" in out.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 60 + 30 * 4 + 10         # coins stake back


def test_a_prop_called_by_hand_isnt_called_twice(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        rid = await night_with_bets(bot)
        await run(bot, "race call", Interaction(**CREW), market="Most coins at the end", winner="Luigi")
        out = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Mario")
        assert "is in the books" in out.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 60 + 30 * 4 + 10 * 2       # the hand call stood


def test_the_panel_posts_itself_only_when_the_race_starts(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="W",
                  runners=", ".join(FIELD))
        await run(bot, "show start", Interaction(**CREW), template="standard")
        talk = await run(bot, "show next", Interaction(**CREW))
        race = await run(bot, "show next", Interaction(**CREW))
        wrap = await run(bot, "show next", Interaction(**CREW))
        return talk, race, wrap

    talk, race, wrap = asyncio.run(go())
    assert len(talk.sent) == 1 and len(wrap.sent) == 1
    assert len(race.sent) == 2 and race.sent[1]["view"] is not None


# --- snapshots -------------------------------------------------------------------

def test_finishing_saves_a_snapshot_and_keeps_only_twenty(fresh_db, monkeypatch):
    db = fresh_db
    folder = db.DB_PATH.parent / "backups"
    folder.mkdir()
    for i in range(25):
        (folder / f"2000-01-01-0000{i:02d}-old.db").write_bytes(b"x")

    async def go():
        bot = await build_bot()
        await night_with_bets(bot)
        await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Luigi")

    asyncio.run(go())
    kept = sorted(folder.glob("*.db"))
    assert len(kept) == DB.KEEP_SNAPSHOTS
    assert kept[-1].name.endswith("Week-9.db")                     # the newest survived
    import sqlite3
    copy = sqlite3.connect(kept[-1])
    assert copy.execute("SELECT status FROM races").fetchone()[0] == "settled"
    copy.close()


def test_a_failed_snapshot_never_blocks_the_payout(fresh_db, monkeypatch):
    db = fresh_db

    def broken(dest):
        raise OSError("disk full")
    monkeypatch.setattr(DB, "snapshot", broken)

    async def go():
        bot = await build_bot()
        await night_with_bets(bot)
        out = await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Luigi")
        assert "is in the books" in out.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 60 + 30 * 4 + 10 * 2
