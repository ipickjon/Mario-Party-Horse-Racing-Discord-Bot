"""A complete broadcast night, driven through the real bot.

The race is built, the rundown runs, viewers guess the order and bet props,
the crew works the control panel through 35 turns, a bonus question opens and
closes, props grade themselves from the tally, the finishing order settles
every guess, and every wallet is checked against a result worked out by hand
from the spec's rules. Then the overlay is checked against the same database.
"""

import asyncio
import re
import time

from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import economy
from tests.harness import Interaction, build_bot, click, edited_text, run

STAFF = dict(user_id=10, name="JaeAIK", staff=True)
HOST = dict(user_id=99, name="host", host=True)
ANA, ROB, KIM = (dict(user_id=20, name="ana"), dict(user_id=30, name="rob"),
                 dict(user_id=40, name="kim"))
FIELD = "Mario, Luigi, Peach, Yoshi"


def staff():
    return Interaction(**STAFF)


def as_(who):
    return Interaction(**who)


def order(*names):
    return dict(zip(B.PLACES, names))


async def press(item, who=STAFF):
    inter = Interaction(**who)
    if await item.interaction_check(inter):
        await item.callback(inter)
    return inter


def buttons(view):
    return {child.item.label: child for child in view.children}


def test_full_broadcast_night(fresh_db):
    db = fresh_db

    async def night():
        bot = await build_bot()

        # --- before the show ---------------------------------------------------
        built = await run(bot, "race create", staff(), week="Week 1", runners=FIELD)
        assert "Week 1** is built" in built.text and "4 runners" in built.text
        assert db.current_season(42)["name"] == "Season 1"     # opened automatically

        dup = await run(bot, "race create", staff(), week="W1b", runners=FIELD)
        assert "already a race in progress" in dup.text

        early = await run(bot, "bet", as_(ANA), amount=10, **order("Mario", "Luigi", "Peach", "Yoshi"))
        assert "That market is closed." in early.text

        nosy = await run(bot, "race open", as_(ROB))
        assert "for the crew" in nosy.text

        started = await run(bot, "show start", staff(), template="standard")
        assert "Betting is now **open**" in started.text

        # --- guesses -----------------------------------------------------------
        jae = await run(bot, "bet", staff(), amount=100, **order("Mario", "Luigi", "Peach", "Yoshi"))
        assert "comes back as 400" in jae.text                 # the spec's own example
        assert db.wallet(10)["balance"] == 0                   # all in is allowed

        short = await run(bot, "bet", as_(ANA), amount=10, **order("Mario", "Luigi", "Peach"))
        assert "Guess the whole order: all 4 runners" in short.text
        twice = await run(bot, "bet", as_(ANA), amount=10, **order("Mario", "Mario", "Peach", "Yoshi"))
        assert "only finish in one place" in twice.text
        bowser = await run(bot, "bet", as_(ANA), amount=10, **order("Mario", "Luigi", "Bowser", "Yoshi"))
        assert "Bowser isn't in this race" in bowser.text
        broke = await run(bot, "bet", as_(ANA), amount=500, **order("Mario", "Luigi", "Peach", "Yoshi"))
        assert "You have 100 points" in broke.text

        await run(bot, "bet", as_(ANA), amount=50, **order("Mario", "Luigi", "Yoshi", "Peach"))
        await run(bot, "prop", as_(ANA), market="Most coins at the end", pick="Peach", amount=20)
        await run(bot, "bet", as_(ROB), amount=60, **order("Luigi", "Mario", "Yoshi", "Peach"))
        await run(bot, "prop", as_(ROB), market="Most ? tiles stepped on", pick="Peach", amount=40)
        await run(bot, "bet", as_(KIM), amount=30, **order("Mario", "Peach", "Yoshi", "Luigi"))
        await run(bot, "prop", as_(KIM), market="Most minigames won", pick="Yoshi", amount=20)

        rules = await run(bot, "payouts", as_(KIM))
        assert "most coins when the race ends" in rules.text
        assert "bonus stars don't decide these" in rules.text
        board = await run(bot, "board", as_(KIM))
        assert "Picked to finish 1st" in board.text

        # --- into the race -----------------------------------------------------
        await run(bot, "show next", staff())
        game = await run(bot, "show next", staff())
        assert "Betting is now **locked**" in game.text
        late = await run(bot, "bet", as_(KIM), amount=5, **order("Yoshi", "Peach", "Luigi", "Mario"))
        assert "closed" in late.text

        # --- control panel -------------------------------------------------------
        panel = await run(bot, "panel", staff())
        pb = buttons(panel.sent[0]["view"])
        assert {"? Mario", "Won: Yoshi", "Next turn", "Undo last"} <= set(pb)
        assert "Crew only." in (await press(pb["? Peach"], ROB)).text
        for _ in range(3):
            await press(pb["? Peach"])
        await press(pb["? Mario"])
        await press(pb["? Mario"])                   # misclick...
        await press(pb["Undo last"])                 # ...undone
        assert db.tallies(db.active_race(42)["id"])["Mario"]["qtiles"] == 1
        for who in ("Won: Yoshi", "Won: Yoshi", "Won: Luigi", "Won: Luigi"):
            await press(pb[who])                      # minigames end tied
        for _ in range(36):
            last = await press(pb["Next turn"])
        assert "Turn 35 of 35" in last.edits[0]["content"]

        # --- bonus question ----------------------------------------------------------
        opened = await run(bot, "bonus open", staff(), question="Who wins the next minigame?",
                           seconds=15)
        bonus_id = db.bonus_markets(db.active_race(42)["id"])[0]["id"]
        assert "Pays **4x**" in opened.text           # fair odds on a four-way pick
        bb = buttons(opened.sent[0]["view"])
        modal = (await press(bb["Mario"], ANA)).modals[0]
        modal.amount._value = "20"
        confirm = as_(ANA)
        await modal.on_submit(confirm)
        assert "20 on **Mario**" in confirm.text
        junk_modal = (await press(bb["Luigi"], KIM)).modals[0]
        junk_modal.amount._value = "lots"
        junk = as_(KIM)
        await junk_modal.on_submit(junk)
        assert "Whole points only." in junk.text

        assert "still taking bets" in (await run(bot, "bonus call", staff(),
                                                 market=str(bonus_id), winner="Mario")).text
        with db.connect() as conn:
            conn.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (time.time() - 1, bonus_id))
        await bot.get_cog("Bonus")._close_later(bonus_id, opened.message, 0)
        assert all(c.item.disabled for c in opened.message.edits[-1]["view"].children)
        assert "That bonus is closed." in (await press(bb["Peach"], ROB)).text
        called = await run(bot, "bonus call", staff(), market=str(bonus_id), winner="Mario")
        assert "paying 4x" in called.text

        # --- post-game -------------------------------------------------------------------
        assert "Still uncalled" in (await run(bot, "race finish", staff())).text

        graded = await run(bot, "race autograde", staff())
        assert "Most ? tiles stepped on** — Peach" in graded.text
        assert "Most minigames won** voided" in graded.text
        await run(bot, "race call", staff(), market="Most coins at the end", winner="Peach")

        partial = await run(bot, "race result", staff(), **order("Mario", "Luigi"))
        assert "Enter the whole order" in partial.text
        result = await run(bot, "race result", staff(), **order("Mario", "Luigi", "Peach", "Yoshi"))
        assert "Final order:** 1st Mario, 2nd Luigi, 3rd Peach, 4th Yoshi" in result.text
        assert "3 of 4 guesses paid out" in result.text
        again = await run(bot, "race result", staff(), **order("Mario", "Luigi", "Peach", "Yoshi"))
        assert "already entered" in again.text

        mine = await run(bot, "mybets", as_(ANA))
        assert "Mario > Luigi > Yoshi > Peach" in mine.text
        assert "Who wins the next minigame?" in mine.text

        done = await run(bot, "race finish", staff())
        assert "PERFECT CARD" in done.text

        for task in list(bot._background):
            task.cancel()
        await asyncio.gather(*bot._background, return_exceptions=True)

    asyncio.run(night())

    # --- every wallet, worked out by hand from the spec ----------------------------
    expected = {
        10: 100 - 100 + 100 * 4,                        # all in, all 4 right
        20: 100 - 50 - 20 - 20 + 50 * 2 + 20 * 2 + 20 * 4,  # 2 right, coins prop, bonus
        30: 100 - 60 - 40 + 40 * 2,                     # none right, ? tiles prop
        40: 100 - 30 - 20 + 30 * 1 + 20,                # 1 right (stake back), tie refunded
    }
    for uid, want in expected.items():
        w = db.wallet(uid)
        assert w["balance"] == want, (uid, w["balance"], want)
        assert w["balance"] == economy.STARTING_BALANCE - w["staked"] + w["returned"]

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bets WHERE settled = 0").fetchone()[0] == 0

    # --- the overlay reads the same night ---------------------------------------------
    from mpr import overlay
    client = TestClient(overlay.app)
    for page in ("tote", "bug", "bonus", "casters", "ads", "standings"):
        assert client.get(f"/{page}").status_code == 200, page
    state = client.get("/state.json").json()
    assert state["race"]["turn"] == 35 and state["race"]["status"] == "settled"
    assert [m["label"] for m in state["markets"]][:4] == [
        "Finishes 1st", "Finishes 2nd", "Finishes 3rd", "Finishes 4th"]
    first = state["markets"][0]["runners"]
    assert {r["name"]: r["staked"] for r in first}["Mario"] == 100 + 50 + 30
    assert [l["multiplier"] for l in state["ladder"]] == [1, 2, 4]
    assert state["standings"][0] == {"name": "JaeAIK", "balance": 400}


def test_mario_kart_sized_field(fresh_db):
    """Twelve racers, no Mario Party props, full-order guesses."""
    db = fresh_db
    racers = [f"Racer{i}" for i in range(1, 13)]

    async def go():
        bot = await build_bot()
        built = await run(bot, "race create", staff(), week="MK1", runners=", ".join(racers),
                          game="Mario Kart", turns=3, props=False)
        assert "12 runners" in built.text and "No props" in built.text
        await run(bot, "race open", staff())
        bet = await run(bot, "bet", as_(ANA), amount=10, **order(*racers))
        assert "comes back as 120" in bet.text
        swapped = racers[:]
        swapped[10], swapped[11] = swapped[11], swapped[10]
        await run(bot, "bet", as_(ROB), amount=10, **order(*swapped))
        prop = await run(bot, "prop", as_(KIM), market="Most coins at the end",
                         pick="Racer1", amount=10)
        assert "no props" in prop.text

        panel = await run(bot, "panel", staff())
        labels = {c.item.label for c in panel.sent[0]["view"].children}
        assert labels == {"Next turn", "Turn back", "Undo last"}   # no tally buttons

        await run(bot, "race lock", staff())
        await run(bot, "race result", staff(), **order(*racers))
        done = await run(bot, "race finish", staff())
        assert "PERFECT CARD" in done.text

    asyncio.run(go())
    assert db.wallet(20)["balance"] == 100 - 10 + 120
    assert db.wallet(30)["balance"] == 100 - 10 + 100          # ten right

    from mpr import overlay
    state = TestClient(overlay.app).get("/state.json").json()
    assert len(state["markets"]) == 12                           # one view per place
    assert all(len(m["runners"]) <= 6 for m in state["markets"])  # board stays readable


def test_reset_is_host_only_and_needs_a_second_click(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", staff(), week="W1", runners=FIELD)
        await run(bot, "race open", staff())
        await run(bot, "bet", as_(ANA), amount=40, **order("Mario", "Luigi", "Peach", "Yoshi"))

        blocked = await run(bot, "reset", Interaction(**HOST), name="Season 2")
        assert "Finish the race in progress" in blocked.text
        await run(bot, "race result", staff(), **order("Mario", "Luigi", "Peach", "Yoshi"))
        for prop in ("Most minigames won", "Most coins at the end", "Most ? tiles stepped on"):
            await run(bot, "race void", staff(), market=prop)
        await run(bot, "race finish", staff())
        assert db.wallet(20)["balance"] == 100 - 40 + 160

        crew = await run(bot, "reset", staff(), name="Season 2")
        assert "Only the server host" in crew.text
        viewer = await run(bot, "reset", as_(ANA), name="Season 2")
        assert "Only the server host" in viewer.text

        asked = await run(bot, "reset", Interaction(**HOST), name="Season 2")
        assert "Sure?" in asked.text and asked.sent[0]["ephemeral"]
        view = asked.sent[0]["view"]
        assert db.wallet(20)["balance"] == 220, "nothing changes until confirmed"

        stranger = await click(view, "Reset everything", as_(ANA))
        assert "Only whoever ran /reset" in stranger.text
        assert db.wallet(20)["balance"] == 220

        done = await click(view, "Reset everything", Interaction(**HOST))
        assert "Season 1 is over" in edited_text(done) and "ana" in edited_text(done)
        assert db.wallet(20)["balance"] == economy.STARTING_BALANCE
        hall = await run(bot, "season hall", as_(ANA))
        assert "Season 1" in hall.text and "220" in hall.text

        cancel_view = (await run(bot, "reset", Interaction(**HOST), name="Season 3")).sent[0]["view"]
        cancelled = await click(cancel_view, "Cancel", Interaction(**HOST))
        assert "Nothing changed" in edited_text(cancelled)
        assert db.current_season(42)["name"] == "Season 2"

    asyncio.run(go())


def test_reset_command_is_hidden_from_non_hosts_in_discord(fresh_db):
    async def go():
        bot = await build_bot()
        reset = next(c for c in bot.tree.walk_commands() if c.qualified_name == "reset")
        return reset.default_permissions

    perms = asyncio.run(go())
    assert perms is not None and perms.manage_guild


def test_rail_money_is_opt_in_and_only_tops_up_the_broke(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        db.wallet(20, "ana"); db.wallet(30, "rob")
        with db.connect() as conn:
            conn.execute("UPDATE wallets SET balance = 0 WHERE user_id = 20")
            conn.execute("UPDATE wallets SET balance = 350 WHERE user_id = 30")
        rail = await run(bot, "railmoney", staff(), week="Week 2")
        assert "Topped 1 of" in rail.text
        assert db.wallet(20)["balance"] == 100 and db.wallet(30)["balance"] == 350
        assert "Nothing paid out" in (await run(bot, "railmoney", staff(), week="Week 2")).text

    asyncio.run(go())


def test_relocking_the_board_leaves_a_running_bonus_alone(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", staff(), week="W", runners=FIELD)
        await run(bot, "race open", staff())
        await run(bot, "race lock", staff())
        rid = db.active_race(42)["id"]
        live = db.open_bonus(rid, "Live one", ["Yes", "No"], 120)
        stale = db.open_bonus(rid, "Stale one", ["Yes", "No"], 120)
        with db.connect() as conn:
            conn.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (time.time() - 5, stale))
        db.lock_expired_bonuses()
        await run(bot, "race lock", staff())
        assert db.market_by_id(live)["status"] == "open"
        assert "20 on **Yes**" in B.submit_bonus_stake(20, "ana", live, 0, "20")
        await run(bot, "race open", staff())
        assert db.market_by_id(stale)["status"] == "locked"
        assert "closed" in B.submit_bonus_stake(20, "ana", stale, 0, "5")

    asyncio.run(go())


def test_refusals_reach_the_player_intact(fresh_db):
    async def go():
        bot = await build_bot()
        for path, kwargs in [
            ("bet", dict(amount=10, **order("Mario", "Luigi", "Peach", "Yoshi"))),
            ("prop", dict(market="Most coins at the end", pick="Mario", amount=10)),
            ("mybets", {}), ("board", {}), ("leaderboard", {}), ("season status", {}),
        ]:
            out = await run(bot, path, as_(ANA), **kwargs)
            assert out.sent and "Something broke" not in out.text, path

    asyncio.run(go())


def test_unexpected_errors_are_caught_and_logged(fresh_db, monkeypatch, caplog):
    async def go():
        bot = await build_bot()
        monkeypatch.setattr(B.db, "wallet", lambda *a, **k: 1 / 0)
        out = await run(bot, "wallet", as_(ANA))
        assert "Something broke on our end" in out.text

    asyncio.run(go())
    assert any("/wallet failed" in r.message for r in caplog.records)


def test_rundown_edges(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "race create", staff(), week="W", runners="A, B, C, D")
        assert "No show template" in (await run(bot, "show start", staff(), template="nope")).text
        await run(bot, "show start", staff(), template="premiere")
        assert "first segment" in (await run(bot, "show back", staff())).text
        for _ in range(3):
            await run(bot, "show next", staff())
        assert "last segment" in (await run(bot, "show next", staff())).text
        assert "Planned total 2h 00m" in (await run(bot, "show rundown", as_(ANA))).text

    asyncio.run(go())


def test_autocomplete_lists(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "race create", staff(), week="W", runners=FIELD)
        inter = as_(ANA)
        assert [c.value for c in await B.entrant_options(inter, "pe")] == ["Peach"]
        assert {c.value for c in await B.template_options(inter, "")} == {"premiere", "standard"}
        rid = db.active_race(42)["id"]
        mid = db.open_bonus(rid, "Yes or no?", ["Yes", "No"], 60)
        assert [c.value for c in await B.bonus_options(inter, "")] == [str(mid)]
        inter.namespace.market = str(mid)
        assert [c.value for c in await B.bonus_answer_options(inter, "")] == ["Yes", "No"]

    asyncio.run(go())


def test_command_tree_is_valid_for_discord(fresh_db):
    async def go():
        bot = await build_bot()
        assert len(bot.tree.get_commands()) <= 100
        rule = re.compile(r"^[-_a-z0-9]{1,32}$")
        for cmd in bot.tree.walk_commands():
            cmd.to_dict(bot.tree)
            assert rule.match(cmd.name), cmd.qualified_name
            assert 1 <= len(cmd.description) <= 100, cmd.qualified_name
            if isinstance(cmd, B.app_commands.Group):
                assert len(cmd.commands) <= 25
            else:
                assert len(cmd.parameters) <= 25
                required_after_optional = False
                seen_optional = False
                for prm in cmd.parameters:
                    assert rule.match(prm.name), (cmd.qualified_name, prm.name)
                    assert len(prm.description) <= 100
                    assert len(prm.choices) <= 25
                    if not prm.required:
                        seen_optional = True
                    elif seen_optional:
                        required_after_optional = True
                assert not required_after_optional, cmd.qualified_name
        return sorted(c.qualified_name for c in bot.tree.walk_commands())

    names = asyncio.run(go())
    for required in ("bet", "prop", "leaderboard", "reset", "race result", "panel",
                     "show next", "bonus open", "race autograde"):
        assert required in names
    assert "season start" not in names          # replaced by /reset
