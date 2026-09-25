"""Load every overlay page in a real browser, in each state it will hit on
broadcast night, and fail on any JavaScript error.

    python tools/check_overlays.py            # checks, writes screenshots to previews/
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["MPR_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "overlay-check.db")
os.environ.setdefault("MPR_OVERLAY_PORT", "8731")
# Run with a key, the way it's deployed, so a page that forgets to pass its
# key on to its data requests shows up here as a blank overlay.
os.environ.setdefault("MPR_OVERLAY_KEY", "overlay-check-key")
sys.path.insert(0, str(ROOT))

import uvicorn                                   # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from mpr import db, overlay                      # noqa: E402

PORT = int(os.environ["MPR_OVERLAY_PORT"])
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "previews"
# The sandbox this was built in can't reach Google Fonts. Those failures are
# expected there and harmless in OBS, which falls back to the next font.
IGNORED = ("fonts.googleapis.com", "fonts.gstatic.com")


def seed():
    db.init()
    db.start_season(42, "Season 1")
    rid = db.create_race(42, "Week 3", "Mario Party", ["Mario", "Luigi", "Peach", "Yoshi"])
    db.start_show(rid, "standard")
    for uid, name in [(1, "JaeAIK"), (2, "ana"), (3, "rob"), (4, "kim"), (5, "sam")]:
        db.wallet(uid, name)
    db.set_featured(1, True, "JaeAIK")
    db.set_featured(3, True, "Rob")
    db.set_race_status(rid, "open")
    for uid, guess, amt in [
        (1, ["Mario", "Luigi", "Peach", "Yoshi"], 60), (2, ["Mario", "Peach", "Luigi", "Yoshi"], 40),
        (3, ["Luigi", "Mario", "Yoshi", "Peach"], 80), (4, ["Peach", "Yoshi", "Mario", "Luigi"], 25),
        (5, ["Mario", "Yoshi", "Luigi", "Peach"], 50),
    ]:
        ok, why = db.place_slate(rid, uid, guess, amt)
        assert ok, why
    coins = db.market(rid, "prop", "coins")
    db.place_bet(rid, coins["id"], 1, "Peach", 20)
    return rid


def set_segment(rid, index, started_ago):
    with db.connect() as conn:
        conn.execute("UPDATE races SET segment = ?, segment_started = ? WHERE id = ?",
                     (index, time.time() - started_ago, rid))


def main() -> int:
    rid = seed()
    server = uvicorn.Server(uvicorn.Config(overlay.app, host="127.0.0.1", port=PORT,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    problems = []
    SHOTS.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        def check(page_name, label, width, height, verify=None, shot=None):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda e: errors.append(f"script error: {e}"))
            page.on("console", lambda m: m.type == "error" and not any(
                h in (m.location or {}).get("url", "") or h in m.text for h in IGNORED)
                and errors.append(f"console: {m.text}"))
            page.goto(f"{BASE}/{page_name}?key={os.environ['MPR_OVERLAY_KEY']}")
            page.wait_for_timeout(2600)
            page.add_style_tag(content="body{background:#1a1c1e}")
            result = verify(page) if verify else None
            if shot:
                page.screenshot(path=str(SHOTS / shot))
            page.close()
            status = "ok" if not errors and result in (None, True) else "FAIL"
            print(f"  {status:<4} {page_name:<10} {label}")
            for e in errors:
                problems.append(f"{page_name} ({label}): {e}")
                print(f"         {e}")
            if result not in (None, True):
                problems.append(f"{page_name} ({label}): {result}")
                print(f"         {result}")

        def text(sel):
            return lambda page: page.inner_text(sel).strip()

        # 1. pre-show, betting open, clock running down
        set_segment(rid, 0, 95)
        check("bug", "pre-show countdown", 520, 300,
              lambda pg: True if pg.inner_text("#big").startswith("8:") else
              f"expected ~8:25 left, got {pg.inner_text('#big')!r}", "bug-preshow.png")
        check("tote", "betting open", 1920, 400,
              lambda pg: True if pg.inner_text("#pays") == "4x" else
              f"price plate shows {pg.inner_text('#pays')!r}, expected 4x", "tote-board.png")

        # 2. segment overrunning
        set_segment(rid, 1, 11 * 60 + 20)
        check("bug", "segment overrun", 520, 300,
              lambda pg: True if pg.inner_text("#big").startswith("+1:") and
              "over" in pg.get_attribute("#big", "class") else
              f"expected overrun, got {pg.inner_text('#big')!r}", "bug-overrun.png")

        # 3. mid-race: last five turns, live tallies with leaders
        set_segment(rid, 2, 60 * 70)
        db.set_race_status(rid, "locked")
        with db.connect() as conn:
            conn.execute("UPDATE races SET turn = 32 WHERE id = ?", (rid,))
        for who, n in [("Peach", 6), ("Mario", 3), ("Luigi", 2), ("Yoshi", 4)]:
            db.bump_tally(rid, who, n, "qtiles")
        for who, n in [("Yoshi", 9), ("Luigi", 7), ("Mario", 8), ("Peach", 5)]:
            db.bump_tally(rid, who, n, "minigames")

        def race_bug(pg):
            if "32" not in pg.inner_text("#big"):
                return f"turn not shown: {pg.inner_text('#big')!r}"
            if pg.is_hidden("#flag"):
                return "last-five-turns flag missing at turn 32 of 35"
            lit = pg.eval_on_selector_all("td.n.lead", "els => els.length")
            return True if lit == 2 else f"expected 2 lit leaders, got {lit}"
        check("bug", "turn 32, last five turns", 520, 420, race_bug, "bug-race.png")

        # 4. bonus question open with money down
        bid = db.open_bonus(rid, "Who wins the next minigame?", ["Mario", "Luigi", "Peach", "Yoshi"], 60)
        for uid, pick, amt in [(2, "Luigi", 20), (3, "Luigi", 15), (4, "Yoshi", 10), (5, "Mario", 6)]:
            db.place_bet(rid, bid, uid, pick, amt)
        check("bonus", "open, counting down", 1280, 460,
              lambda pg: True if "up" in pg.get_attribute("#band", "class") and
              pg.inner_text("#clock").startswith("0:5") else
              f"band not up or clock wrong: {pg.inner_text('#clock')!r}", "bonus-open.png")

        # 5. bonus settled, winner lit
        with db.connect() as conn:
            conn.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (time.time() - 1, bid))
        db.lock_expired_bonuses()
        db.call_market_id(bid, "Luigi")
        check("bonus", "settled, winner shown", 1280, 460,
              lambda pg: True if pg.eval_on_selector_all(".opt.win", "e => e.length") == 1 else
              "winner row not lit", "bonus-settled.png")

        # 6. nothing running: the band must hide itself
        with db.connect() as conn:
            conn.execute("UPDATE markets SET called_at = ? WHERE id = ?", (time.time() - 60, bid))
        check("bonus", "idle, hidden", 1280, 460,
              lambda pg: True if "up" not in (pg.get_attribute("#band", "class") or "") else
              "band still showing with no bonus")

        # 7. the rest
        check("casters", "crew slips", 1100, 460, None, "caster-slips.png")
        check("ads", "ad rotation", 1280, 260, None, "ad-slot.png")
        check("standings", "season table", 900, 620, None, "standings.png")
        browser.close()

    server.should_exit = True
    print()
    if problems:
        print(f"{len(problems)} problem(s).")
        return 1
    print("Every page loaded clean in every state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
