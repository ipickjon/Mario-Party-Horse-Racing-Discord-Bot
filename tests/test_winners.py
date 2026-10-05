"""The winners reveal: what it shows after a night closes."""

import asyncio

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import overlay
from tests.harness import Interaction, build_bot, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
FIELD = ["Mario", "Luigi", "Peach", "Yoshi"]
PLAYERS = {20: "ana", 30: "rob", 40: "kim", 50: "sam", 60: "pat", 70: "lee"}


def order(*names):
    return dict(zip(B.PLACES, names))


def night(fresh_db, bets, props=()):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(**CREW), mode="Mario Party", week="Week 5",
                  runners=", ".join(FIELD))
        await run(bot, "race open", Interaction(**CREW))
        for uid, guess, amount in bets:
            await run(bot, "bet", Interaction(user_id=uid, name=PLAYERS[uid]), amount=amount,
                      **order(*guess))
        for uid, pick, amount in props:
            await run(bot, "sidebet", Interaction(user_id=uid, name=PLAYERS[uid]),
                      market="Most coins at the end", pick=pick, amount=amount)
        live = TestClient(overlay.app).get("/state.json").json()["results"]
        assert live is None, "nothing to reveal while the race is still going"
        await run(bot, "race result", Interaction(**CREW), **order(*FIELD), coins="Peach")

    asyncio.run(go())
    return db.night_results(db.latest_race(42)["id"])


def test_winners_ranked_by_profit_across_every_bet(fresh_db):
    res = night(fresh_db, bets=[
        (20, FIELD, 40),                                     # 4 right: 40 -> 160
        (30, ["Mario", "Luigi", "Yoshi", "Peach"], 50),      # 2 right: 50 -> 100
        (40, ["Luigi", "Mario", "Yoshi", "Peach"], 30),      # none right
    ], props=[(40, "Peach", 40)])                            # kim's prop: 40 -> 80
    # kim lost 30 on the order but won 40 on the prop: +10 only if props count
    assert [(w["name"], w["profit"]) for w in res["winners"]] == \
        [("ana", 120), ("rob", 50), ("kim", 10)]
    assert res["perfect"] == ["ana"] and res["players"] == 3
    assert res["finish"] == FIELD
    assert {p["label"]: p["winner"] for p in res["props"]}["Most coins at the end"] == "Peach"


def test_break_even_isnt_a_win_and_the_list_stops_at_five(fresh_db):
    many = [(uid, FIELD, 10 + uid // 10) for uid in PLAYERS]           # six perfect cards
    many.append((20, ["Mario", "Peach", "Yoshi", "Luigi"], 5))          # ana's 1-right push
    res = night(fresh_db, bets=many)
    assert len(res["winners"]) == 5 and len(res["perfect"]) == 6
    profits = [w["profit"] for w in res["winners"]]
    assert profits == sorted(profits, reverse=True)


def test_nobody_ahead_still_produces_a_card(fresh_db):
    res = night(fresh_db, bets=[(20, ["Luigi", "Mario", "Yoshi", "Peach"], 30)])
    assert res["winners"] == [] and res["perfect"] == []


def test_winners_page_and_obs_link(fresh_db):
    night(fresh_db, bets=[(20, FIELD, 40)])
    client = TestClient(overlay.app)
    assert client.get("/winners").status_code == 200
    assert client.get("/state.json").json()["results"]["winners"][0]["name"] == "ana"
    assert any(name == "winners" for name, *_ in B.OVERLAY_SOURCES)
