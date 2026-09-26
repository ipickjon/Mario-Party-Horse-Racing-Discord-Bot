"""Overlay server. Add these as OBS browser sources.

    /bug         corner scorebug: segment clock, turn count, live tallies
    /bonus       mid-game bonus question; hides itself when none is running
    /tote        lower-third board: prices and where the money is
    /casters     the crew's own slips
    /ads         rotating fake ad slot
    /standings   season leaderboard for the wrap-up
    /state.json  everything the pages poll
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, economy

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
ADS_FILE = ROOT / "config" / "ads.json"
ADS_IMAGES = ROOT / "config" / "ads"

GUILD_ID = int(os.getenv("MPR_GUILD_ID", "0")) or None

# Optional. When set, every page and /state.json need ?key=<this> in the URL,
# so only your OBS can read the overlay once it's on the public internet.
OVERLAY_KEY = os.getenv("MPR_OVERLAY_KEY", "")


def listen_address() -> tuple[str, int]:
    """Local: 127.0.0.1:8730. On Railway (which sets PORT): 0.0.0.0:$PORT so
    the public domain reaches it. MPR_OVERLAY_HOST/PORT override either."""
    port = os.getenv("MPR_OVERLAY_PORT") or os.getenv("PORT") or "8730"
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    return os.getenv("MPR_OVERLAY_HOST", default_host), int(port)


def public_url(page: str = "tote") -> str:
    """The URL to paste into OBS, as best we can tell from the environment."""
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    host, port = listen_address()
    base = f"https://{domain}" if domain else f"http://127.0.0.1:{port}"
    return f"{base}/{page}" + (f"?key={OVERLAY_KEY}" if OVERLAY_KEY else "")

# How long a settled bonus stays on screen so the casters can call it.
BONUS_LINGER = 25

app = FastAPI(title="Mario Party Horse Racing overlay",
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_key(request: Request, call_next):
    if OVERLAY_KEY and request.url.path != "/healthz":
        given = request.query_params.get("key", "")
        if not secrets.compare_digest(given, OVERLAY_KEY):
            return PlainTextResponse("Missing or wrong overlay key. Add ?key=... to the URL.",
                                     status_code=403)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}
ADS_IMAGES.mkdir(parents=True, exist_ok=True)
app.mount("/ads/img", StaticFiles(directory=ADS_IMAGES), name="ad-images")


def load_ads() -> dict:
    """The live rotation, straight from the database, so an ad added or
    removed in Discord shows up on the next poll with no restart."""
    slots = []
    for a in db.ads(status="live"):
        if a["has_upload"]:
            image_url = f"/ads/uploaded/{a['id']}"
        elif a["image_file"]:
            image_url = f"/ads/img/{a['image_file']}"
        else:
            image_url = None
        slots.append({"id": a["id"], "headline": a["headline"], "body": a["body"],
                      "tag": a["tag"], "accent": a["accent"], "weight": a["weight"],
                      "image_url": image_url})
    return {"dwell_seconds": db.ad_dwell_seconds(), "slots": slots}


@app.get("/ads/uploaded/{ad_id}")
def uploaded_ad_image(ad_id: int):
    """An image sent in through /ad add. Only live ads are served, so nothing
    waiting for review can be reached from outside."""
    a = db.ad(ad_id)
    image = db.ad_image(ad_id) if a is not None and a["status"] == "live" else None
    if image is None:
        return PlainTextResponse("No such ad.", status_code=404)
    blob, media_type = image
    return Response(content=blob, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=300"})


def current_race():
    if GUILD_ID:
        return db.active_race(GUILD_ID) or db.latest_race(GUILD_ID)
    # No guild configured: fall back to the newest race anywhere rather than
    # showing a blank overlay, which is the failure nobody notices until air.
    return db.latest_race(None)


def bonus_payload(race_id: int, meta: dict, now: float) -> dict | None:
    """The bonus to put on screen: an open one, or one just settled."""
    for m in db.bonus_markets(race_id):
        live = m["result"] is None and m["status"] == "open" and now < (m["closes_at"] or 0)
        waiting = m["result"] is None and not live
        recent = m["result"] is not None and now - (m["called_at"] or 0) < BONUS_LINGER
        if not (live or waiting or recent):
            continue
        totals = db.market_totals(m["id"])
        staked = sum(totals.values())
        return {
            "id": m["id"],
            "question": m["label"],
            "multiplier": m["multiplier"],
            "state": "open" if live else ("locked" if waiting else "settled"),
            "closes_at": m["closes_at"],
            "result": m["result"],
            "staked": staked,
            "tickets": db.market_tickets(m["id"]),
            "options": [
                {"name": o, "color": meta.get(o, ("#5c7a68", 0))[0],
                 "slot": meta.get(o, (None, 0))[1],
                 "staked": totals.get(o, 0),
                 "share": totals.get(o, 0) / staked if staked else 0}
                for o in db.options_of(m)
            ],
        }
    return None


@app.get("/state.json")
def state():
    now = time.time()
    ads = load_ads()
    race = current_race()
    if race is None:
        return JSONResponse({"race": None, "ads": ads, "now": now})

    ents = db.entrants(race["id"])
    names = [e["name"] for e in ents]
    meta = {e["name"]: (e["color"], e["slot"]) for e in ents}
    counts = db.tallies(race["id"])

    # The tote board cycles through one view per finishing place (how much
    # money has each runner there), then the props. With a big field it
    # shows the six runners with the most money on each place.
    records = db.form(names, race["mode"], race["guild_id"])
    markets = []
    slate = db.slate_market(race["id"])
    n = len(names)
    tiers = economy.ladder_table(n)
    for place, money in enumerate(db.slate_views(race["id"]), start=1):
        staked = sum(money.values())
        shown = names if n <= 6 else sorted(names, key=lambda x: -money.get(x, 0))[:6]
        markets.append({
            "label": f"Finishes {db.ordinal(place)}", "kind": "position", "key": str(place),
            "status": slate["status"] if slate else "closed",
            "result": None, "staked": staked, "multiplier": None,
            "runners": [{"name": x, "color": meta[x][0], "slot": meta[x][1],
                         "staked": money.get(x, 0), "form": records[x]["recent"],
                         "share": money.get(x, 0) / staked if staked else 0} for x in shown],
        })
    for m in db.markets(race["id"], ("prop",)):
        totals = db.market_totals(m["id"])
        staked = sum(totals.values())
        shown = names if n <= 6 else sorted(names, key=lambda x: -totals.get(x, 0))[:6]
        markets.append({
            "label": db.label_of(m), "kind": m["kind"], "key": m["key"],
            "status": m["status"], "result": m["result"], "staked": staked,
            "multiplier": m["multiplier"],
            "runners": [{"name": x, "color": meta[x][0], "slot": meta[x][1],
                         "staked": totals.get(x, 0), "form": records[x]["recent"],
                         "share": totals.get(x, 0) / staked if staked else 0} for x in shown],
        })

    casters = []
    for w in db.featured_users():
        casters.append({
            "name": w["on_air_name"] or w["display_name"],
            "balance": w["balance"],
            "bets": [{"market": b["label"] or db.market_label(b["kind"], b["key"]),
                      "kind": b["kind"], "pick": b["pick_text"],
                      "color": meta.get(b["selection"][0] if b["kind"] == "slate"
                                        else b["selection"], ("#8899aa", 0))[0],
                      "amount": b["amount"], "payout": b["payout"],
                      "settled": bool(b["settled"])}
                     for b in db.user_bets(race["id"], w["user_id"])],
        })

    show = db.show_state(race)
    season = db.current_season(race["guild_id"])
    return JSONResponse({
        "now": now,
        "season": ({"name": season["name"], "done": db.season_races(season["id"]),
                    "length": economy.SEASON_LENGTH} if season else None),
        "race": {"week": race["week_label"], "game": race["game"], "status": race["status"],
                 "turn": race["turn"], "total_turns": race["total_turns"],
                 "mode": race["mode"], "unit": db.MODES[race["mode"]]["unit"]},
        "show": ({"current": show["current"], "next": show["next"],
                  "index": show["index"], "count": len(show["segments"]),
                  "started": show["started"]} if show else None),
        "entrants": [{"name": e["name"], "color": e["color"], "slot": e["slot"]} for e in ents],
        "tallies": [{"name": n, "color": meta.get(n, ("#8899aa", 0))[0],
                     "slot": meta.get(n, (None, 0))[1], **c} for n, c in counts.items()],
        "leaders": {
            key: economy.tally_leader({n: c[key] for n, c in counts.items()})
            for key in db.TALLIED_PROPS
        },
        "bonus": bonus_payload(race["id"], meta, now),
        "callouts": db.recent_callouts(race["id"]),
        "results": db.night_results(race["id"]),
        "ladder": [{"correct": k, "multiplier": mult, "chance": chance}
                   for k, mult, chance in tiers],
        "field_size": n,
        "prop_multiplier": economy.PROP_MULTIPLIER,
        "markets": markets,
        "casters": casters,
        "ads": ads,
        "standings": [{"name": r["display_name"] or str(r["user_id"]), "balance": r["balance"]}
                      for r in db.leaderboard(10)],
    })


def _page(name: str):
    return FileResponse(WEB / f"{name}.html")


for _name in ("tote", "standings", "casters", "ads", "bug", "bonus", "winners"):
    app.add_api_route(f"/{_name}", (lambda n=_name: _page(n)), methods=["GET"])


async def serve():
    """Run the overlay inside the bot's event loop."""
    db.init()
    host, port = listen_address()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    db.init()
    host, port = listen_address()
    uvicorn.run(app, host=host, port=port)
