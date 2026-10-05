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
    # two finished weeks, so the tote board has form to show
    for week, finish in [("Week 1", ["Yoshi", "Mario", "Luigi", "Peach"]),
                         ("Week 2", ["Mario", "Yoshi", "Peach", "Luigi"])]:
        past = db.create_race(42, week, "Mario Party", finish)
        db.set_race_status(past, "open")
        db.settle_night(past, finish, "tie")
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


def banner_png(width=1170, height=170) -> bytes:
    """A striped test banner, drawn without an image library."""
    import struct
    import zlib

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = b""
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            stripe = ((x + y) // 40) % 2
            row += bytes((88, 101, 242) if stripe else (255, 168, 30))
        rows += bytes(row)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


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

        def check(page_name, label, width, height, verify=None, shot=None, wait=2600):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda e: errors.append(f"script error: {e}"))
            page.on("console", lambda m: m.type == "error" and not any(
                h in (m.location or {}).get("url", "") or h in m.text for h in IGNORED)
                and errors.append(f"console: {m.text}"))
            page.goto(f"{BASE}/{page_name}?key={os.environ['MPR_OVERLAY_KEY']}")
            page.wait_for_timeout(wait)
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
        def tote(pg):
            if pg.inner_text("#pays") != "4x":
                return f"price plate shows {pg.inner_text('#pays')!r}, expected 4x"
            forms = pg.eval_on_selector_all(
                ".form", "els => els.map(e => e.innerText.split(/\\s+/).join(' ').trim())")
            # latest first: Mario won week 2 and was 2nd in week 1
            want = ["1 2", "4 3", "3 4", "2 1"]
            return True if forms == want else f"form column: {forms}, expected {want}"
        check("tote", "betting open, with form", 1920, 400, tote, "tote-board.png")

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

        # 3b. a name callout: fresh all-in shows, an old one isn't replayed on load
        with db.connect() as conn:
            conn.execute("DELETE FROM callouts")          # start from a clean feed
            for who, age in [("rob", 120), ("ana", 1)]:
                conn.execute(
                    """INSERT INTO callouts (race_id, bet_id, who, amount, what, all_in, created_at)
                       VALUES (?, 0, ?, 100, 'Peach to win', 1, ?)""",
                    (rid, who, time.time() - age))

        def shout(pg):
            if pg.is_hidden("#shout"):
                return "callout strip not showing"
            if pg.inner_text("#shoutTag") != "ALL IN" or pg.inner_text("#shoutWho") != "ana":
                return f"showed {pg.inner_text('#shoutTag')!r} for {pg.inner_text('#shoutWho')!r}"
            return True
        check("bug", "all-in callout", 520, 500, shout, "bug-callout.png")
        with db.connect() as conn:
            conn.execute("DELETE FROM callouts")

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
        db.call_market_id(bid, ["Luigi", "Yoshi"])               # a 2 v 2
        check("bonus", "settled, two winners lit", 1280, 460,
              lambda pg: True if pg.eval_on_selector_all(".opt.win", "e => e.length") == 2 else
              f"lit rows: {pg.eval_on_selector_all('.opt.win', 'e => e.length')}", "bonus-settled.png")

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

        # 8. a Mario Kart race: laps, and no ? tile table
        kart = db.create_race(42, "MK Night", "Mario Kart", [f"Racer{i}" for i in range(1, 13)],
                              mode="kart")
        db.start_show(kart, "standard")
        set_segment(kart, 2, 60)
        with db.connect() as conn:
            conn.execute("UPDATE races SET turn = 2 WHERE id = ?", (kart,))

        def kart_bug(pg):
            if "laps" not in pg.inner_text("#big"):
                return f"expected laps, got {pg.inner_text('#big')!r}"
            if not pg.is_hidden("#table"):
                return "tally table showing for Kart"
            return True if pg.is_hidden("#flag") else "last-five-turns flag showing for Kart"
        check("bug", "Mario Kart, lap 2 of 3", 520, 300, kart_bug, "bug-kart.png")

        # 9. an ad someone uploaded through Discord, alone in the rotation
        for a in db.ads():
            db.remove_ad(a["id"])
        db.add_ad(headline="Community banner", body="", tag="made by ana", accent="#5865f2",
                  weight=1, status="live", submitted_by=20, submitted_name="ana",
                  image_blob=banner_png(), image_type="image/png")

        def image_ad(pg):
            if pg.is_hidden("#shot"):
                return "uploaded image not shown"
            width = pg.eval_on_selector("#shot", "img => img.naturalWidth")
            return True if width == 1170 else f"image didn't load (naturalWidth {width})"
        check("ads", "uploaded image ad", 1280, 260, image_ad, "ad-uploaded.png")
        # 9b. ad text always fits the slot, however long
        cases = [
            ("the headline that overflowed on stream",
             "HEADLINE: YOSHI DEADLOCK GETS SECOND IN MARIO PARTY", "", "made by ToadyHawk"),
            ("the longest ad allowed",
             "W" * 60, "M" * 120, "made by someone with a long name"),
            ("one 60-letter word", "Supercalifragilistic" * 3, "", "made by ana"),
            ("a short ad keeps full size", "Join the Discord", "discord.gg/example", ""),
        ]
        fit_js = """() => {
            const r = el => el.getBoundingClientRect();
            const slot = r(document.getElementById('slot'));
            const parts = ['headline', 'body'].map(id => document.getElementById(id))
                                              .filter(el => !el.hidden && el.textContent);
            const tag = document.getElementById('tag');
            const inside = parts.every(el => { const b = r(el);
                return b.left >= slot.left - 1 && b.right <= slot.right + 1 &&
                       b.top >= slot.top - 1 && b.bottom <= slot.bottom + 1; });
            const hit = tag.textContent && parts.some(el => { const a = r(el), t = r(tag);
                return !(a.right <= t.left || a.left >= t.right || a.bottom <= t.top || a.top >= t.bottom); });
            const copy = document.getElementById('copy');
            return { inside, hit: !!hit,
                     size: parseFloat(getComputedStyle(document.getElementById('headline')).fontSize),
                     spills: copy.scrollHeight > copy.clientHeight + 1 && !copy.classList.contains('clamped') };
        }"""
        for label, headline, body, tag in cases:
            for a in db.ads():
                db.remove_ad(a["id"])
            db.add_ad(headline=headline, body=body, tag=tag, accent="#ffa81e", weight=1,
                      status="live", submitted_by=1, submitted_name="t")

            def fits(pg, short=(label == "a short ad keeps full size")):
                m = pg.evaluate(fit_js)
                if not m["inside"] or m["spills"]:
                    return f"text escapes the slot: {m}"
                if m["hit"]:
                    return f"text runs into the corner tag: {m}"
                if m["size"] < 26:
                    return f"shrank below readable: {m['size']}px"
                if short and m["size"] != 50:
                    return f"short ad shrank to {m['size']}px"
                return True
            name = "ad-fit-" + label.split()[1] + ".png"
            check("ads", label, 1280, 260, fits, name)

        # 9c. the ad slot fills whatever size the OBS source is; images are never cropped
        for a in db.ads():
            db.remove_ad(a["id"])
        db.add_ad(headline="HEADLINE: YOSHI DEADLOCK GETS SECOND IN MARIO PARTY", body="",
                  tag="made by ToadyHawk", accent="#ffa81e", weight=1, status="live",
                  submitted_by=1, submitted_name="t")
        for w, h in [(1280, 260), (900, 200), (1600, 320)]:
            def fills(pg, w=w, h=h):
                box = pg.evaluate("() => { const r = document.getElementById('slot').getBoundingClientRect();"
                                  " return [r.left, r.top, r.right, r.bottom]; }")
                if box[0] < 0 or box[1] < 0 or box[2] > w or box[3] > h:
                    return f"slot {box} doesn't fit a {w}x{h} source"
                if box[2] - box[0] < w - 40:
                    return f"slot only {box[2] - box[0]:.0f}px wide in a {w}px source"
                m = pg.evaluate(fit_js)
                return True if m["inside"] and not m["spills"] and not m["hit"] else f"text: {m}"
            check("ads", f"fills a {w}x{h} source", w, h, fills, f"ad-size-{w}.png")

        for a in db.ads():
            db.remove_ad(a["id"])
        db.add_ad(headline="Square art", body="", tag="made by ana", accent="#5865f2", weight=1,
                  status="live", submitted_by=20, submitted_name="ana",
                  image_blob=banner_png(300, 300), image_type="image/png")
        check("ads", "a square image is shown whole", 1280, 260,
              lambda pg: True if pg.eval_on_selector("#shot", "i => getComputedStyle(i).objectFit") == "contain"
              and pg.eval_on_selector("#shot", "i => i.naturalWidth") == 300 else "image cropped or missing",
              "ad-square.png")

        # 9d. a bet left open until betting locks shows "Open", not a countdown
        shown = db.latest_race(None)["id"]                  # the race the overlay is showing
        uid = db.open_bonus(shown, "First to land on the bank?", ["Mario", "Luigi", "Peach", "Yoshi"],
                            None, None, "character", 2)
        with db.connect() as conn:
            conn.execute("UPDATE markets SET status = 'open' WHERE id = ?", (uid,))
        check("bonus", "open until betting locks", 1280, 460,
              lambda pg: True if pg.inner_text("#clock") == "Open" else f"clock: {pg.inner_text('#clock')!r}")
        db.call_market_id(uid, None)
        with db.connect() as conn:
            conn.execute("UPDATE markets SET called_at = 0 WHERE id = ?", (uid,))

        # 10. the winners reveal, after a night with a perfect card
        win = db.create_race(42, "Week 4", "Mario Party", ["Mario", "Luigi", "Peach", "Yoshi"])
        db.set_race_status(win, "open")
        for uid, guess, amt in [(2, ["Mario", "Luigi", "Peach", "Yoshi"], 40),
                                (4, ["Mario", "Luigi", "Yoshi", "Peach"], 30),
                                (5, ["Mario", "Peach", "Yoshi", "Luigi"], 20),
                                (3, ["Luigi", "Mario", "Yoshi", "Peach"], 5)]:
            ok, why = db.place_slate(win, uid, guess, amt)
            assert ok, why
        db.settle_night(win, ["Mario", "Luigi", "Peach", "Yoshi"], "Peach")

        def winners(pg):
            names = pg.eval_on_selector_all(".row.in .who", "els => els.map(e => e.innerText)")
            if names != ["ana", "kim"]:
                return f"revealed rows: {names}"
            if pg.inner_text(".row.top .profit") != "+120":
                return f"top profit shows {pg.inner_text('.row.top .profit')!r}"
            if pg.is_hidden("#perfect") or "ana" not in pg.inner_text("#perfectWho"):
                return "perfect card banner missing"
            return True if "Most coins at the end" in pg.inner_text("#props") else "props rail empty"
        check("winners", "reveal after a perfect card", 1920, 1080, winners, "winners.png", wait=6500)

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
