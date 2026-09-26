"""The form guide: each runner's recent finishes."""

import asyncio
import json

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import overlay
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA = dict(user_id=20, name="ana")
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]


def past_race(db, finish, mode="party", week="W"):
    """A finished race with a known result, straight into the database."""
    names = finish if mode == "party" else finish
    rid = db.create_race(42, week, "Mario Party", names, mode=mode)
    db.set_race_status(rid, "open")
    db.settle_order(rid, finish)
    for key in ("minigames", "coins", "qtiles"):
        if db.market(rid, "prop", key):
            db.call_market(rid, "prop", key, None)
    db.finish_race(rid)
    return rid


def test_form_is_latest_first_with_wins_and_average(fresh_db):
    db = fresh_db
    past_race(db, ["Yoshi", "Mario", "Luigi", "Peach"], week="1")
    past_race(db, ["Mario", "Yoshi", "Peach", "Luigi"], week="2")
    past_race(db, ["Yoshi", "Peach", "Mario", "Luigi"], week="3")
    rec = db.form(FIELD, "party")
    assert rec["Yoshi"]["recent"] == [1, 2, 1]            # week 3, 2, 1
    assert rec["Yoshi"]["wins"] == 2 and rec["Yoshi"]["starts"] == 3
    assert rec["Mario"]["recent"] == [3, 1, 2]             # not a palindrome, unlike Yoshi's
    assert rec["Mario"]["average"] == 2.0


def test_form_keeps_modes_apart_and_skips_voided_results(fresh_db):
    db = fresh_db
    past_race(db, ["Mario", "Luigi", "Peach", "Yoshi"], week="party")
    kart = ["Mario"] + [f"R{i}" for i in range(7)]
    past_race(db, list(reversed(kart)), mode="kart", week="kart")   # Mario last in Kart
    voided = db.create_race(42, "void", "MP", FIELD)
    db.set_race_status(voided, "open")
    db.settle_order(voided, None)
    for key in ("minigames", "coins", "qtiles"):
        db.call_market(voided, "prop", key, None)
    db.finish_race(voided)
    assert db.form(["Mario"], "party")["Mario"]["recent"] == [1]
    assert db.form(["Mario"], "kart")["Mario"]["recent"] == [8]


def test_form_ignores_capitals_and_stops_at_five(fresh_db):
    db = fresh_db
    for week in range(7):
        past_race(db, ["mario", "luigi", "peach", "yoshi"], week=str(week))
    rec = db.form(["Mario"], "party")["Mario"]
    assert rec["recent"] == [1, 1, 1, 1, 1] and rec["starts"] == 7


def test_form_command_and_debuts(fresh_db):
    db = fresh_db
    past_race(db, ["Yoshi", "Mario", "Luigi", "Peach"], week="1")

    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="Week 2",
                  runners="Yoshi, Mario, Luigi, Wario")
        out = await run(bot, "form", Interaction(**ANA))
        board = await run(bot, "board", Interaction(**ANA))
        return out, board

    out, board = asyncio.run(go())
    assert "**Yoshi**: 1st (1 win from 1, average 1.0)" in out.text
    assert "**Wario**: debut, no form yet" in out.text
    assert out.sent[0]["ephemeral"] and "Form, latest first" in board.text


def test_overlay_carries_form_for_the_tote_board(fresh_db):
    db = fresh_db
    past_race(db, ["Yoshi", "Mario", "Luigi", "Peach"], week="1")
    db.create_race(42, "Week 2", "Mario Party", ["Yoshi", "Mario", "Luigi", "Wario"])
    runners = TestClient(overlay.app).get("/state.json").json()["markets"][0]["runners"]
    assert {r["name"]: r["form"] for r in runners} == {
        "Yoshi": [1], "Mario": [2], "Luigi": [3], "Wario": []}
