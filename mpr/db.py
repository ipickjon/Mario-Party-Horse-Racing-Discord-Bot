"""SQLite storage. One file, WAL mode, short-lived connections.

Both the bot and the overlay server read this database, so nothing holds a
connection open longer than a single operation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import economy

ROOT = Path(__file__).resolve().parent.parent
def resolve_db_path() -> Path:
    """Where the season lives.

    MPR_DB_PATH wins if set (point a test server at a scratch file). On
    Railway, an attached volume is used automatically. Otherwise ./data.
    Without a volume on Railway, every redeploy would wipe the season.
    """
    if os.getenv("MPR_DB_PATH"):
        return Path(os.environ["MPR_DB_PATH"])
    if os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        return Path(os.environ["RAILWAY_VOLUME_MOUNT_PATH"]) / "horserace.db"
    return ROOT / "data" / "horserace.db"


DB_PATH = resolve_db_path()
SHOW_FILE = ROOT / "config" / "show.json"

PROP_KEYS = {
    "minigames": "Most minigames won",
    "coins": "Most coins at the end",
    "qtiles": "Most ? tiles stepped on",
}
SLATE_KEY = "order"
SLATE_LABEL = "Finishing order"
# Props the bot can grade on its own from the live tally.
TALLIED_PROPS = ("qtiles", "minigames")
DEFAULT_TURNS = 35
PALETTE = ["#e5453a", "#3fae5a", "#f4a43c", "#5b8dd9", "#b36ad6", "#e8d44d",
           "#4cc3c9", "#f07fb0", "#9c7a4f", "#8fd35f", "#6b7fe3", "#d9d9d9"]


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def market_label(kind: str, key: str) -> str:
    if kind == "slate":
        return SLATE_LABEL
    return PROP_KEYS.get(key, key)


def label_of(row) -> str:
    """Label for any market row, including bonus markets."""
    if row["label"]:
        return row["label"]
    return market_label(row["kind"], row["key"])


SCHEMA = """
CREATE TABLE IF NOT EXISTS seasons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    started_at REAL    NOT NULL,
    ended_at   REAL,
    status     TEXT    NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS hall (
    season_id    INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    display_name TEXT    NOT NULL,
    balance      INTEGER NOT NULL,
    PRIMARY KEY (season_id, rank)
);

CREATE TABLE IF NOT EXISTS races (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    season_id       INTEGER REFERENCES seasons(id),
    week_label      TEXT    NOT NULL,
    game            TEXT    NOT NULL DEFAULT 'Mario Party',
    status          TEXT    NOT NULL DEFAULT 'draft',
    created_at      REAL    NOT NULL,
    locked_at       REAL,
    settled_at      REAL,
    turn            INTEGER NOT NULL DEFAULT 0,
    total_turns     INTEGER NOT NULL DEFAULT 35,
    rundown         TEXT,
    segment         INTEGER NOT NULL DEFAULT -1,
    segment_started REAL
);

CREATE TABLE IF NOT EXISTS entrants (
    race_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    slot     INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    color    TEXT    NOT NULL DEFAULT '#8899aa',
    PRIMARY KEY (race_id, slot)
);

CREATE TABLE IF NOT EXISTS markets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id     INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'closed',
    result      TEXT,
    label       TEXT,
    options     TEXT,
    multiplier  INTEGER,
    opened_at   REAL,
    closes_at   REAL,
    called_at   REAL,
    channel_id  INTEGER,
    message_id  INTEGER,
    UNIQUE (race_id, kind, key)
);

CREATE TABLE IF NOT EXISTS bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    market_id  INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    selection  TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    placed_at  REAL    NOT NULL,
    payout     INTEGER,
    settled    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bets_market ON bets(market_id);
CREATE INDEX IF NOT EXISTS idx_bets_user   ON bets(user_id);

CREATE TABLE IF NOT EXISTS wallets (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT    NOT NULL DEFAULT '',
    balance      INTEGER NOT NULL,
    staked       INTEGER NOT NULL DEFAULT 0,
    returned     INTEGER NOT NULL DEFAULT 0,
    last_stipend TEXT    NOT NULL DEFAULT '',
    featured     INTEGER NOT NULL DEFAULT 0,
    on_air_name  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tallies (
    race_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    entrant   TEXT    NOT NULL,
    qtiles    INTEGER NOT NULL DEFAULT 0,
    minigames INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (race_id, entrant)
);

-- Every tally press and turn change, so a misclick mid-show can be undone.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    entrant    TEXT,
    delta      INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    undone     INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release. init() adds any that are missing so
# a database from an earlier test run keeps working.
_MIGRATIONS = {
    "races": {
        "season_id": "INTEGER",
        "turn": "INTEGER NOT NULL DEFAULT 0",
        "total_turns": "INTEGER NOT NULL DEFAULT 35",
        "rundown": "TEXT",
        "segment": "INTEGER NOT NULL DEFAULT -1",
        "segment_started": "REAL",
    },
    "markets": {
        "label": "TEXT", "options": "TEXT", "multiplier": "INTEGER",
        "opened_at": "REAL", "closes_at": "REAL", "called_at": "REAL",
        "channel_id": "INTEGER", "message_id": "INTEGER",
    },
    "tallies": {"minigames": "INTEGER NOT NULL DEFAULT 0"},
    "wallets": {
        "featured": "INTEGER NOT NULL DEFAULT 0",
        "on_air_name": "TEXT NOT NULL DEFAULT ''",
    },
}


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def snapshot(dest: Path) -> Path:
    """A consistent copy of the whole database, safe to take mid-show."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(DB_PATH, timeout=10)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


def init():
    with connect() as conn:
        # Older databases: add columns before the schema's indexes reference them.
        for table, cols in _MIGRATIONS.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- wallets


def wallet(user_id: int, display_name: str = "") -> sqlite3.Row:
    with connect() as conn:
        row = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO wallets (user_id, display_name, balance) VALUES (?,?,?)",
                (user_id, display_name, economy.STARTING_BALANCE),
            )
            row = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        elif display_name and row["display_name"] != display_name:
            conn.execute(
                "UPDATE wallets SET display_name = ? WHERE user_id = ?", (display_name, user_id)
            )
        return row


def adjust_balance(conn, user_id: int, delta: int, *, staked=0, returned=0):
    conn.execute(
        """UPDATE wallets SET balance = balance + ?, staked = staked + ?,
                              returned = returned + ? WHERE user_id = ?""",
        (delta, staked, returned, user_id),
    )


def top_up_wallets(week_label: str) -> dict:
    """Bring anyone below the rail floor back up to it, once per week label.

    Deliberately not a payment to everyone: see the note on RAIL_FLOOR in
    economy.py for why that choice decides what the leaderboard measures.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, balance FROM wallets WHERE last_stipend != ?", (week_label,)
        ).fetchall()
        topped = coins = 0
        for row in rows:
            amount = economy.top_up(row["balance"])
            conn.execute(
                "UPDATE wallets SET balance = balance + ?, last_stipend = ? WHERE user_id = ?",
                (amount, week_label, row["user_id"]),
            )
            if amount:
                topped += 1
                coins += amount
        return {"checked": len(rows), "topped": topped, "coins": coins}


def set_featured(user_id: int, on: bool, on_air_name: str = "") -> None:
    wallet(user_id)
    with connect() as conn:
        conn.execute(
            "UPDATE wallets SET featured = ?, on_air_name = ? WHERE user_id = ?",
            (1 if on else 0, on_air_name, user_id),
        )


def featured_users() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM wallets WHERE featured = 1 ORDER BY on_air_name, display_name"
        ).fetchall()


def leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """SELECT user_id, display_name, balance, staked, returned
                 FROM wallets ORDER BY balance DESC LIMIT ?""",
            (limit,),
        ).fetchall()


# ----------------------------------------------------------------- seasons


def current_season(guild_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM seasons WHERE guild_id = ? AND status = 'running'
                ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()


def season_races(season_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM races WHERE season_id = ? AND status = 'settled'",
            (season_id,),
        ).fetchone()["n"]


def start_season(guild_id: int, name: str) -> dict:
    """Close the running season, archive its table, reset every wallet."""
    closing = current_season(guild_id)
    champions = []
    if closing is not None:
        standings = leaderboard(50)
        with connect() as conn:
            for rank, row in enumerate(standings, start=1):
                conn.execute(
                    """INSERT OR REPLACE INTO hall (season_id, rank, user_id, display_name, balance)
                       VALUES (?,?,?,?,?)""",
                    (closing["id"], rank, row["user_id"],
                     row["display_name"] or str(row["user_id"]), row["balance"]),
                )
            conn.execute(
                "UPDATE seasons SET status = 'closed', ended_at = ? WHERE id = ?",
                (time.time(), closing["id"]),
            )
            conn.execute(
                "UPDATE wallets SET balance = ?, staked = 0, returned = 0, last_stipend = ''",
                (economy.STARTING_BALANCE,),
            )
        champions = [dict(r) for r in standings[:3]]
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO seasons (guild_id, name, started_at) VALUES (?,?,?)",
            (guild_id, name, time.time()),
        )
        return {"season_id": cur.lastrowid,
                "closed": closing["name"] if closing else None,
                "champions": champions}


def hall_of_fame(guild_id: int, top: int = 3) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.name AS season, h.rank, h.display_name, h.balance
                 FROM hall h JOIN seasons s ON s.id = h.season_id
                WHERE s.guild_id = ? AND h.rank <= ? ORDER BY s.id DESC, h.rank""",
            (guild_id, top),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ races


def create_race(
    guild_id: int, week_label: str, game: str, names: list[str],
    total_turns: int = DEFAULT_TURNS, props: bool = True,
) -> int:
    """names is the field in slot order, 2 to 12 runners. props=False skips
    the Mario Party side bets, for a game like Mario Kart where they don't fit."""
    if not economy.MIN_RUNNERS <= len(names) <= economy.MAX_RUNNERS:
        raise ValueError("field size")
    with connect() as conn:
        season = conn.execute(
            """SELECT id FROM seasons WHERE guild_id = ? AND status = 'running'
                ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO races (guild_id, season_id, week_label, game, created_at, total_turns)
               VALUES (?,?,?,?,?,?)""",
            (guild_id, season["id"] if season else None, week_label, game,
             time.time(), total_turns),
        )
        race_id = cur.lastrowid
        for slot, name in enumerate(names, start=1):
            conn.execute(
                "INSERT INTO entrants (race_id, slot, name, color) VALUES (?,?,?,?)",
                (race_id, slot, name, PALETTE[(slot - 1) % len(PALETTE)]),
            )
            conn.execute("INSERT INTO tallies (race_id, entrant) VALUES (?,?)", (race_id, name))
        conn.execute(
            "INSERT INTO markets (race_id, kind, key, label) VALUES (?, 'slate', ?, ?)",
            (race_id, SLATE_KEY, SLATE_LABEL),
        )
        if props:
            for key, label in PROP_KEYS.items():
                conn.execute(
                    """INSERT INTO markets (race_id, kind, key, label, multiplier)
                       VALUES (?, 'prop', ?, ?, ?)""",
                    (race_id, key, label, economy.PROP_MULTIPLIER),
                )
        return race_id


def race(race_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()


def active_race(guild_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM races WHERE guild_id = ? AND status IN ('draft','open','locked')
                ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()


def latest_race(guild_id: int | None) -> sqlite3.Row | None:
    """Most recent race for a guild. None means any guild, which is what the
    overlay falls back to if MPR_GUILD_ID was never set."""
    with connect() as conn:
        if guild_id:
            return conn.execute(
                "SELECT * FROM races WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,)
            ).fetchone()
        return conn.execute("SELECT * FROM races ORDER BY id DESC LIMIT 1").fetchone()


def set_race_status(race_id: int, status: str):
    column = {"locked": "locked_at", "settled": "settled_at"}.get(status)
    with connect() as conn:
        conn.execute("UPDATE races SET status = ? WHERE id = ?", (status, race_id))
        if column:
            conn.execute(f"UPDATE races SET {column} = ? WHERE id = ?", (time.time(), race_id))
        market_status = {"open": "open", "locked": "locked"}.get(status)
        if market_status:
            # Bonus markets run on their own clock; opening or locking the main
            # board must never reopen a bonus that already closed.
            conn.execute(
                """UPDATE markets SET status = ?
                    WHERE race_id = ? AND result IS NULL AND kind IN ('slate','prop')""",
                (market_status, race_id),
            )


def entrants(race_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM entrants WHERE race_id = ? ORDER BY slot", (race_id,)
        ).fetchall()


def entrant_by_slot(race_id: int, slot: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM entrants WHERE race_id = ? AND slot = ?", (race_id, slot)
        ).fetchone()


def markets(race_id: int, kinds: tuple[str, ...] = ("slate", "prop")) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(kinds))
    with connect() as conn:
        return conn.execute(
            f"""SELECT * FROM markets WHERE race_id = ? AND kind IN ({marks})
                 ORDER BY CASE kind WHEN 'slate' THEN 0 WHEN 'prop' THEN 1 ELSE 2 END,
                          key, id""",
            (race_id, *kinds),
        ).fetchall()


def market(race_id: int, kind: str, key: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM markets WHERE race_id = ? AND kind = ? AND key = ?",
            (race_id, kind, key),
        ).fetchone()


def slate_market(race_id: int) -> sqlite3.Row | None:
    return market(race_id, "slate", SLATE_KEY)


def market_by_id(market_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()


def options_of(row) -> list[str]:
    """What a market can be settled on: its own list for a bonus, else the field."""
    if row["options"]:
        return json.loads(row["options"])
    return [e["name"] for e in entrants(row["race_id"])]


# ------------------------------------------------------------------- bets


def _is_accepting(m, now: float) -> bool:
    if m["status"] != "open" or m["result"] is not None:
        return False
    return m["closes_at"] is None or now < m["closes_at"]


def _decode(kind: str, raw: str):
    return json.loads(raw) if kind == "slate" else raw


def place_bet(
    race_id: int, market_id: int, user_id: int, selection, amount: int
) -> tuple[bool, str]:
    now = time.time()
    with connect() as conn:
        m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or not _is_accepting(m, now):
            return False, "That market is closed."
        w = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        balance = w["balance"] if w else 0
        already = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM bets
                WHERE market_id = ? AND user_id = ? AND settled = 0""",
            (market_id, user_id),
        ).fetchone()["total"]
        ok, reason = economy.check_wager(balance, already, amount)
        if not ok:
            return False, reason
        stored = json.dumps(selection) if m["kind"] == "slate" else selection
        conn.execute(
            """INSERT INTO bets (race_id, market_id, user_id, selection, amount, placed_at)
               VALUES (?,?,?,?,?,?)""",
            (race_id, market_id, user_id, stored, amount, now),
        )
        adjust_balance(conn, user_id, -amount, staked=amount)
        return True, ""


def check_order(race_id: int, guess: list[str]) -> str:
    """Why a finishing-order guess is invalid, or '' if it's fine."""
    field = [e["name"] for e in entrants(race_id)]
    if len(guess) != len(field):
        return (f"Guess the whole order: all {len(field)} runners, 1st to "
                f"{ordinal(len(field))}. Tonight: {', '.join(field)}.")
    unknown = [g for g in guess if g not in field]
    if unknown:
        return f"{unknown[0]} isn't in this race. Tonight: {', '.join(field)}."
    if len(set(guess)) != len(guess):
        return "Each runner can only finish in one place."
    return ""


def place_slate(race_id: int, user_id: int, guess: list[str], amount: int) -> tuple[bool, str]:
    problem = check_order(race_id, guess)
    if problem:
        return False, problem
    return place_bet(race_id, slate_market(race_id)["id"], user_id, guess, amount)


def market_totals(market_id: int) -> dict[str, int]:
    """Money on each pick. For the finishing order, money on each runner to
    win, which is what people mean by 'the favourite'."""
    totals: dict[str, int] = {}
    with connect() as conn:
        m = conn.execute("SELECT kind FROM markets WHERE id = ?", (market_id,)).fetchone()
        rows = conn.execute(
            "SELECT selection, amount FROM bets WHERE market_id = ?", (market_id,)
        ).fetchall()
    for r in rows:
        pick = json.loads(r["selection"])[0] if m and m["kind"] == "slate" else r["selection"]
        totals[pick] = totals.get(pick, 0) + r["amount"]
    return totals


def slate_views(race_id: int) -> list[dict[str, int]]:
    """For each finishing place, how much money has each runner there."""
    m = slate_market(race_id)
    n = len(entrants(race_id))
    views = [dict() for _ in range(n)]
    if m is None:
        return views
    with connect() as conn:
        rows = conn.execute(
            "SELECT selection, amount FROM bets WHERE market_id = ?", (m["id"],)
        ).fetchall()
    for r in rows:
        for place, name in enumerate(json.loads(r["selection"])):
            if place < n:
                views[place][name] = views[place].get(name, 0) + r["amount"]
    return views


def market_tickets(market_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM bets WHERE market_id = ?", (market_id,)
        ).fetchone()["n"]


def user_bets(race_id: int, user_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, m.kind, m.key, m.label FROM bets b
                 JOIN markets m ON m.id = b.market_id
                WHERE b.race_id = ? AND b.user_id = ? ORDER BY b.id""",
            (race_id, user_id),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["selection"] = _decode(r["kind"], r["selection"])
        d["pick_text"] = (" > ".join(d["selection"]) if r["kind"] == "slate"
                          else d["selection"])
        out.append(d)
    return out


def open_tickets(race_id: int) -> list[economy.Ticket]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.id, b.user_id, m.kind, m.key, b.selection, b.amount
                 FROM bets b JOIN markets m ON m.id = b.market_id
                WHERE b.race_id = ? AND b.settled = 0""",
            (race_id,),
        ).fetchall()
    return [economy.Ticket(r["id"], r["user_id"], r["kind"], r["key"],
                           _decode(r["kind"], r["selection"]), r["amount"]) for r in rows]


def _apply(conn, payouts: dict[int, int]):
    for bet_id, amount in payouts.items():
        conn.execute("UPDATE bets SET payout = ?, settled = 1 WHERE id = ?", (amount, bet_id))
        if amount:
            owner = conn.execute(
                "SELECT user_id FROM bets WHERE id = ?", (bet_id,)
            ).fetchone()["user_id"]
            adjust_balance(conn, owner, amount, returned=amount)


def _names() -> dict[int, str]:
    with connect() as conn:
        return {r["user_id"]: r["display_name"]
                for r in conn.execute("SELECT user_id, display_name FROM wallets")}


def settle_order(race_id: int, finish: list[str] | None) -> dict:
    """Enter the final order and pay every placement guess. None voids and
    refunds them all."""
    m = slate_market(race_id)
    if m is None:
        return {"error": "No finishing-order market on this race."}
    if m["result"] is not None:
        return {"error": "The finishing order was already entered."}
    if finish is not None:
        problem = check_order(race_id, finish)
        if problem:
            return {"error": problem.replace("Guess the whole order", "Enter the whole order")}

    tickets = [t for t in open_tickets(race_id) if t.kind == "slate"]
    graded = economy.grade_slates(tickets, finish)
    with connect() as conn:
        conn.execute(
            "UPDATE markets SET result = ?, status = 'settled', called_at = ? WHERE id = ?",
            (json.dumps(finish) if finish else "VOID", time.time(), m["id"]),
        )
        _apply(conn, {bet_id: back for bet_id, (_, back) in graded.items()})

    names = _names()
    by_ticket = {t.bet_id: t for t in tickets}
    results = sorted(
        ({"user_id": by_ticket[b].user_id, "name": names.get(by_ticket[b].user_id, "?"),
          "right": right, "staked": by_ticket[b].amount, "returned": back}
         for b, (right, back) in graded.items()),
        key=lambda r: (-r["returned"], -r["right"]),
    )
    return {"label": SLATE_LABEL, "kind": "slate", "finish": finish,
            "voided": finish is None, "tickets": len(tickets), "results": results}


def call_market_id(market_id: int, winner: str | None) -> dict:
    """Settle a single-pick market (a prop or a bonus) and pay it now."""
    m = market_by_id(market_id)
    if m is None:
        return {"error": "No such market."}
    if m["kind"] == "slate":
        return {"error": "Use /race result to enter the finishing order."}
    label = label_of(m)
    if m["result"] is not None:
        return {"error": f"{label} was already called."}
    if winner is not None and winner not in options_of(m):
        return {"error": f"{winner} isn't an option on {label}."}

    with connect() as conn:
        conn.execute(
            "UPDATE markets SET result = ?, status = 'settled', called_at = ? WHERE id = ?",
            (winner if winner is not None else "VOID", time.time(), m["id"]),
        )
    tickets = [t for t in open_tickets(m["race_id"]) if t.kind == m["kind"] and t.key == m["key"]]
    payouts = economy.grade_flat(tickets, m["key"], winner, m["multiplier"])
    with connect() as conn:
        _apply(conn, payouts)
    return {"label": label, "winner": winner, "kind": m["kind"], "voided": winner is None,
            "tickets": len(tickets),
            "winners": 0 if winner is None else sum(1 for v in payouts.values() if v),
            "paid": sum(payouts.values()), "multiplier": m["multiplier"]}


def call_market(race_id: int, kind: str, key: str, winner: str | None) -> dict:
    m = market(race_id, kind, key)
    if m is None:
        return {"error": "No such market on this race."}
    return call_market_id(m["id"], winner)


def uncalled(race_id: int) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM markets WHERE race_id = ? AND result IS NULL ORDER BY id", (race_id,)
        ).fetchall()
    return [label_of(r) for r in rows]


def finish_race(race_id: int) -> dict:
    """Close the night once every market has a result."""
    r = race(race_id)
    if r is None:
        return {"error": "No such race."}
    if r["status"] == "settled":
        return {"error": f"{r['week_label']} is already settled."}
    missing = uncalled(race_id)
    if missing:
        return {"error": "Still uncalled: " + ", ".join(missing)}

    m = slate_market(race_id)
    finish = None if m["result"] == "VOID" else json.loads(m["result"])
    names = _names()
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, selection, amount, payout FROM bets WHERE market_id = ?", (m["id"],)
        ).fetchall()
    guesses = sorted(
        ({"name": names.get(x["user_id"], "?"),
          "right": economy.positions_right(json.loads(x["selection"]), finish) if finish else 0,
          "staked": x["amount"], "returned": x["payout"] or 0} for x in rows),
        key=lambda g: (-g["returned"], -g["right"]),
    )
    set_race_status(race_id, "settled")
    return {"finish": finish, "guesses": guesses}


# ----------------------------------------------------------------- reset


def reset_economy(guild_id: int, name: str) -> dict:
    """/reset: archive the current table into the hall of fame, put every
    wallet back to the starting balance, and open a new season under name."""
    return start_season(guild_id, name)


# ----------------------------------------------------------- bonus markets


def open_bonus(
    race_id: int, question: str, options: list[str], seconds: int, multiplier: int | None = None
) -> int:
    """A quick mid-game market with its own clock, independent of the main board."""
    now = time.time()
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM markets WHERE race_id = ? AND kind = 'bonus'", (race_id,)
        ).fetchone()["n"]
        cur = conn.execute(
            """INSERT INTO markets
                 (race_id, kind, key, status, label, options, multiplier, opened_at, closes_at)
               VALUES (?, 'bonus', ?, 'open', ?, ?, ?, ?, ?)""",
            (race_id, f"b{n + 1}", question, json.dumps(options),
             multiplier or economy.bonus_multiplier(len(options)), now, now + seconds),
        )
        return cur.lastrowid


def attach_message(market_id: int, channel_id: int, message_id: int):
    with connect() as conn:
        conn.execute(
            "UPDATE markets SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, market_id),
        )


def lock_expired_bonuses() -> list[sqlite3.Row]:
    """Flip any bonus past its clock to locked. Bets already refuse after the
    clock runs out; this just makes the stored status agree."""
    now = time.time()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM markets WHERE kind = 'bonus' AND status = 'open'
                AND closes_at IS NOT NULL AND closes_at <= ?""",
            (now,),
        ).fetchall()
        conn.execute(
            """UPDATE markets SET status = 'locked' WHERE kind = 'bonus' AND status = 'open'
                AND closes_at IS NOT NULL AND closes_at <= ?""",
            (now,),
        )
        return rows


def bonus_markets(race_id: int, include_settled: bool = True) -> list[sqlite3.Row]:
    with connect() as conn:
        sql = "SELECT * FROM markets WHERE race_id = ? AND kind = 'bonus'"
        if not include_settled:
            sql += " AND result IS NULL"
        return conn.execute(sql + " ORDER BY id DESC", (race_id,)).fetchall()


# ------------------------------------------------------------ live tallies


def _log(conn, race_id: int, kind: str, entrant: str | None, delta: int):
    conn.execute(
        "INSERT INTO events (race_id, kind, entrant, delta, created_at) VALUES (?,?,?,?,?)",
        (race_id, kind, entrant, delta, time.time()),
    )


def bump_tally(race_id: int, entrant: str, delta: int = 1, field: str = "qtiles") -> int:
    if field not in TALLIED_PROPS:
        raise ValueError(field)
    with connect() as conn:
        conn.execute(
            "INSERT INTO tallies (race_id, entrant) VALUES (?,?) ON CONFLICT DO NOTHING",
            (race_id, entrant),
        )
        before = conn.execute(
            f"SELECT {field} FROM tallies WHERE race_id = ? AND entrant = ?", (race_id, entrant)
        ).fetchone()[0]
        after = max(0, before + delta)
        conn.execute(
            f"UPDATE tallies SET {field} = ? WHERE race_id = ? AND entrant = ?",
            (after, race_id, entrant),
        )
        if after != before:
            _log(conn, race_id, field, entrant, after - before)
        return after


def bump_turn(race_id: int, delta: int = 1) -> int:
    with connect() as conn:
        r = conn.execute("SELECT turn, total_turns FROM races WHERE id = ?", (race_id,)).fetchone()
        after = max(0, min(r["total_turns"], r["turn"] + delta))
        if after != r["turn"]:
            conn.execute("UPDATE races SET turn = ? WHERE id = ?", (after, race_id))
            _log(conn, race_id, "turn", None, after - r["turn"])
        return after


def undo_last(race_id: int) -> dict | None:
    """Reverse the most recent tally press or turn change."""
    with connect() as conn:
        ev = conn.execute(
            """SELECT * FROM events WHERE race_id = ? AND undone = 0
                ORDER BY id DESC LIMIT 1""",
            (race_id,),
        ).fetchone()
        if ev is None:
            return None
        if ev["kind"] == "turn":
            conn.execute("UPDATE races SET turn = MAX(0, turn - ?) WHERE id = ?",
                         (ev["delta"], race_id))
        else:
            conn.execute(
                f"UPDATE tallies SET {ev['kind']} = MAX(0, {ev['kind']} - ?) "
                "WHERE race_id = ? AND entrant = ?",
                (ev["delta"], race_id, ev["entrant"]),
            )
        conn.execute("UPDATE events SET undone = 1 WHERE id = ?", (ev["id"],))
        return dict(ev)


def tallies(race_id: int) -> dict[str, dict[str, int]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.entrant, t.qtiles, t.minigames FROM tallies t
                 LEFT JOIN entrants e ON e.race_id = t.race_id AND e.name = t.entrant
                WHERE t.race_id = ? ORDER BY e.slot""",
            (race_id,),
        ).fetchall()
        return {r["entrant"]: {"qtiles": r["qtiles"], "minigames": r["minigames"]} for r in rows}


def autograde(race_id: int) -> list[dict]:
    """Settle the tallied props from the live counts. A tie voids rather than
    guessing. Props already called by hand are left alone."""
    counts = tallies(race_id)
    out = []
    for key in TALLIED_PROPS:
        m = market(race_id, "prop", key)
        if m is None or m["result"] is not None:
            continue
        leader = economy.tally_leader({n: c[key] for n, c in counts.items()})
        summary = call_market_id(m["id"], leader)
        summary["counts"] = {n: c[key] for n, c in counts.items()}
        out.append(summary)
    return out


# ---------------------------------------------------------------- rundown


def show_templates() -> dict[str, list[dict]]:
    try:
        return json.loads(SHOW_FILE.read_text()).get("templates", {})
    except (OSError, ValueError):
        return {}


def _apply_action(race_id: int, segment: dict) -> str | None:
    action = segment.get("action")
    r = race(race_id)
    if action == "open" and r["status"] in ("draft", "locked"):
        set_race_status(race_id, "open")
        return "open"
    if action == "lock" and r["status"] == "open":
        set_race_status(race_id, "locked")
        return "lock"
    return None


def start_show(race_id: int, template: str) -> dict:
    segments = show_templates().get(template)
    if not segments:
        return {"error": f"No show template called {template!r} in config/show.json."}
    with connect() as conn:
        conn.execute(
            "UPDATE races SET rundown = ?, segment = 0, segment_started = ? WHERE id = ?",
            (json.dumps(segments), time.time(), race_id),
        )
    return {"segment": segments[0], "index": 0, "count": len(segments),
            "action": _apply_action(race_id, segments[0])}


def move_segment(race_id: int, step: int = 1) -> dict:
    r = race(race_id)
    if not r["rundown"]:
        return {"error": "No show running. `/show start` first."}
    segments = json.loads(r["rundown"])
    index = r["segment"] + step
    if index < 0:
        return {"error": "Already on the first segment."}
    if index >= len(segments):
        return {"error": "That was the last segment."}
    with connect() as conn:
        conn.execute(
            "UPDATE races SET segment = ?, segment_started = ? WHERE id = ?",
            (index, time.time(), race_id),
        )
    # Only fire a segment's action when moving forward onto it.
    action = _apply_action(race_id, segments[index]) if step > 0 else None
    return {"segment": segments[index], "index": index, "count": len(segments), "action": action}


def show_state(race_row) -> dict | None:
    if not race_row["rundown"]:
        return None
    segments = json.loads(race_row["rundown"])
    i = race_row["segment"]
    return {
        "segments": segments,
        "index": i,
        "current": segments[i] if 0 <= i < len(segments) else None,
        "next": segments[i + 1] if 0 <= i + 1 < len(segments) else None,
        "started": race_row["segment_started"],
    }
