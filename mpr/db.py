"""SQLite storage. One file, WAL mode, short-lived connections.

Both the bot and the overlay server read this database, so nothing holds a
connection open longer than a single operation.
"""

from __future__ import annotations

import json
import os
import re
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
    "firststar": "First to get a star",
}
SLATE_KEY = "order"
SLATE_LABEL = "Finishing order"
# Props the bot can grade on its own from the live tally.
TALLIED_PROPS = ("qtiles", "minigames")
DEFAULT_TURNS = 20
PALETTE = ["#e5453a", "#3fae5a", "#f4a43c", "#5b8dd9", "#b36ad6", "#e8d44d",
           "#4cc3c9", "#f07fb0", "#9c7a4f", "#8fd35f", "#6b7fe3", "#d9d9d9"]


# Game modes. The mode decides the field size, whether the Mario Party props
# run, and which version of /bet and /race result the server sees.
MODES = {
    "party": {"label": "Mario Party", "runners": (4, 4), "props": True,
              "turns": 20, "unit": "turn"},
    "kart": {"label": "Mario Kart", "runners": (2, 12), "props": False,
             "turns": 3, "unit": "lap"},
}


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
    turn            INTEGER NOT NULL DEFAULT 1,
    total_turns     INTEGER NOT NULL DEFAULT 35,
    rundown         TEXT,
    segment         INTEGER NOT NULL DEFAULT -1,
    segment_started REAL,
    mode            TEXT    NOT NULL DEFAULT 'party'
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
    bonus_type  TEXT,
    max_picks   INTEGER,
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
    settled    INTEGER NOT NULL DEFAULT 0,
    comeback   INTEGER NOT NULL DEFAULT 0
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
    on_air_name  TEXT    NOT NULL DEFAULT '',
    adjusted     INTEGER NOT NULL DEFAULT 0
);

-- Big bets and all-ins, announced on the scorebug and in Discord.
CREATE TABLE IF NOT EXISTS callouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL,
    bet_id     INTEGER NOT NULL,
    who        TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    what       TEXT    NOT NULL,
    all_in     INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL
);

-- Every /gift, so the crew can always see who gave what and why.
CREATE TABLE IF NOT EXISTS gifts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    giver_id   INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS tallies (
    race_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    entrant   TEXT    NOT NULL,
    qtiles    INTEGER NOT NULL DEFAULT 0,
    minigames INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (race_id, entrant)
);

-- Every tally press and turn change, so a misclick mid-show can be undone.
-- The ad rotation. Crew ads go straight in as 'live'; viewer submissions
-- wait as 'pending' until the crew approves them. Uploaded images are stored
-- in the row itself, so /backup carries them and they never go stale.
CREATE TABLE IF NOT EXISTS ads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    headline       TEXT    NOT NULL,
    body           TEXT    NOT NULL DEFAULT '',
    tag            TEXT    NOT NULL DEFAULT '',
    accent         TEXT    NOT NULL DEFAULT '#ffa81e',
    weight         INTEGER NOT NULL DEFAULT 1,
    image_file     TEXT,
    image_blob     BLOB,
    image_type     TEXT,
    status         TEXT    NOT NULL DEFAULT 'live',
    submitted_by   INTEGER,
    submitted_name TEXT    NOT NULL DEFAULT '',
    created_at     REAL    NOT NULL,
    reviewed_by    INTEGER,
    pinned         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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
        "turn": "INTEGER NOT NULL DEFAULT 1",
        "total_turns": "INTEGER NOT NULL DEFAULT 35",
        "rundown": "TEXT",
        "segment": "INTEGER NOT NULL DEFAULT -1",
        "segment_started": "REAL",
        "mode": "TEXT NOT NULL DEFAULT 'party'",
    },
    "markets": {
        "label": "TEXT", "options": "TEXT", "multiplier": "INTEGER",
        "opened_at": "REAL", "closes_at": "REAL", "called_at": "REAL",
        "channel_id": "INTEGER", "message_id": "INTEGER",
        "bonus_type": "TEXT", "max_picks": "INTEGER",
    },
    "bets": {"comeback": "INTEGER NOT NULL DEFAULT 0"},
    "ads": {"pinned": "INTEGER NOT NULL DEFAULT 0"},
    "tallies": {"minigames": "INTEGER NOT NULL DEFAULT 0"},
    "wallets": {
        "featured": "INTEGER NOT NULL DEFAULT 0",
        "on_air_name": "TEXT NOT NULL DEFAULT ''",
        "adjusted": "INTEGER NOT NULL DEFAULT 0",
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
    _seed_ads_once()


# -------------------------------------------------------------------- ads

ADS_FILE = ROOT / "config" / "ads.json"


def _seed_ads_once():
    """Bring config/ads.json into the database the first time only. After
    that the rotation is managed with /ad commands, and removing every ad
    doesn't bring the samples back on the next restart."""
    try:
        slots = json.loads(ADS_FILE.read_text()).get("slots", [])
    except (OSError, ValueError):
        slots = []
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM meta WHERE key = 'ads_pinned'").fetchone():
            # One-time update for servers seeded before pinning existed: pin
            # the starting ads that ads.json marks as pinned (the Discord promo).
            for slot in slots:
                if slot.get("pinned"):
                    conn.execute("""UPDATE ads SET pinned = 1
                                     WHERE submitted_name = 'config/ads.json' AND headline = ?""",
                                 (slot.get("headline") or slot.get("image"),))
            conn.execute("INSERT INTO meta (key, value) VALUES ('ads_pinned', '1')")
        if conn.execute("SELECT 1 FROM meta WHERE key = 'ads_seeded'").fetchone():
            return
        for slot in slots:
            if not (slot.get("headline") or slot.get("image")):
                continue
            conn.execute(
                """INSERT INTO ads (headline, body, tag, accent, weight, image_file,
                                    status, submitted_name, created_at, pinned)
                   VALUES (?,?,?,?,?,?, 'live', 'config/ads.json', ?, ?)""",
                (slot.get("headline") or slot.get("image"), slot.get("body", ""),
                 slot.get("tag", ""), slot.get("accent", "#ffa81e"),
                 max(1, min(10, int(slot.get("weight", 1)))), slot.get("image"), time.time(),
                 1 if slot.get("pinned") else 0),
            )
        conn.execute("INSERT INTO meta (key, value) VALUES ('ads_seeded', '1')")


def ad_dwell_seconds() -> int:
    try:
        return max(4, int(json.loads(ADS_FILE.read_text()).get("dwell_seconds", 12)))
    except (OSError, ValueError, TypeError):
        return 12


def add_ad(*, headline: str, body: str, tag: str, accent: str, weight: int, status: str,
           submitted_by: int | None, submitted_name: str,
           image_blob: bytes | None = None, image_type: str | None = None,
           pinned: bool = False) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO ads (headline, body, tag, accent, weight, image_blob, image_type,
                                status, submitted_by, submitted_name, created_at, pinned)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (headline, body, tag, accent, weight, image_blob, image_type, status,
             submitted_by, submitted_name, time.time(), 1 if pinned else 0),
        )
        return cur.lastrowid


_AD_COLUMNS = ("id, headline, body, tag, accent, weight, image_file, image_type, status, "
               "submitted_by, submitted_name, created_at, pinned, "
               "image_blob IS NOT NULL AS has_upload")


def set_pinned(ad_id: int, pinned: bool) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE ads SET pinned = ? WHERE id = ?",
                            (1 if pinned else 0, ad_id)).rowcount == 1


def ad(ad_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(f"SELECT {_AD_COLUMNS} FROM ads WHERE id = ?", (ad_id,)).fetchone()


def ads(status: str | None = None, submitted_by: int | None = None) -> list[sqlite3.Row]:
    sql, args = f"SELECT {_AD_COLUMNS} FROM ads WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if submitted_by is not None:
        sql += " AND submitted_by = ?"
        args.append(submitted_by)
    with connect() as conn:
        return conn.execute(sql + " ORDER BY id", args).fetchall()


def ad_image(ad_id: int) -> tuple[bytes, str] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT image_blob, image_type FROM ads WHERE id = ?", (ad_id,)
        ).fetchone()
    if row is None or row["image_blob"] is None:
        return None
    return bytes(row["image_blob"]), row["image_type"]


def approve_ad(ad_id: int, reviewer: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE ads SET status = 'live', reviewed_by = ? WHERE id = ? AND status = 'pending'",
            (reviewer, ad_id),
        )
        return cur.rowcount == 1


def remove_ad(ad_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM ads WHERE id = ?", (ad_id,)).rowcount == 1


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


def gift(user_id: int, display_name: str, amount: int, giver_id: int,
         reason: str = "") -> tuple[int, int]:
    """Give (or, with a negative amount, take back) points. A balance never
    goes below zero. Returns (the change actually made, the new balance)."""
    wallet(user_id, display_name)
    with connect() as conn:
        balance = conn.execute(
            "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
        ).fetchone()["balance"]
        change = max(amount, -balance)
        if change == 0:
            return 0, balance          # nothing happened, so nothing to log
        conn.execute(
            "UPDATE wallets SET balance = balance + ?, adjusted = adjusted + ? WHERE user_id = ?",
            (change, change, user_id),
        )
        conn.execute(
            """INSERT INTO gifts (user_id, giver_id, amount, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (user_id, giver_id, change, reason, time.time()),
        )
        return change, balance + change


def rank_of(user_id: int) -> tuple[int, int]:
    """(place, number of players). Tied balances share a place."""
    with connect() as conn:
        mine = conn.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
        if mine is None:
            return total + 1, total + 1
        ahead = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE balance > ?", (mine["balance"],)
        ).fetchone()[0]
        return ahead + 1, total


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
                "UPDATE wallets SET balance = ?, staked = 0, returned = 0, adjusted = 0, "
                "last_stipend = ''",
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
    total_turns: int | None = None, props: bool | None = None, mode: str = "party",
) -> int:
    """names is the field in slot order. The mode sets the allowed field size
    and whether the Mario Party props run, unless props is given."""
    low, high = MODES[mode]["runners"]
    if not low <= len(names) <= high:
        raise ValueError("field size")
    if props is None:
        props = MODES[mode]["props"]
    if total_turns is None:
        total_turns = MODES[mode]["turns"]
    with connect() as conn:
        season = conn.execute(
            """SELECT id FROM seasons WHERE guild_id = ? AND status = 'running'
                ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO races (guild_id, season_id, week_label, game, created_at,
                                  total_turns, mode, turn)
               VALUES (?,?,?,?,?,?,?,1)""",
            (guild_id, season["id"] if season else None, week_label, game,
             time.time(), total_turns, mode),
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
            if status == "locked":
                # Bonus bets left open "until betting locks" close with the board.
                # (Opening the board never reopens a bonus.)
                conn.execute(
                    """UPDATE markets SET status = 'locked'
                        WHERE race_id = ? AND kind = 'bonus' AND status = 'open'
                          AND result IS NULL AND closes_at IS NULL""",
                    (race_id,),
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


def bonus_taking_bets(m, now: float | None = None) -> bool:
    """A bonus is open until its timer runs out, or, with no timer, until
    betting locks for the race."""
    now = time.time() if now is None else now
    return (m["result"] is None and m["status"] == "open"
            and (m["closes_at"] is None or now < m["closes_at"]))


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
        if m["kind"] == "bonus" and m["max_picks"]:
            picked = {r["selection"] for r in conn.execute(
                "SELECT DISTINCT selection FROM bets WHERE market_id = ? AND user_id = ?",
                (market_id, user_id))}
            if selection not in picked and len(picked) >= m["max_picks"]:
                if m["max_picks"] == 1:
                    return False, f"You've already backed {next(iter(picked))}. One side only on this one."
                return False, (f"You can back at most {m['max_picks']} answers on this one, and "
                               f"you've picked {', '.join(sorted(picked))}.")
        stored = json.dumps(selection) if m["kind"] == "slate" else selection
        cur = conn.execute(
            """INSERT INTO bets (race_id, market_id, user_id, selection, amount, placed_at)
               VALUES (?,?,?,?,?,?)""",
            (race_id, market_id, user_id, stored, amount, now),
        )
        adjust_balance(conn, user_id, -amount, staked=amount)
        kind = economy.callout_kind(balance, amount)
        if kind is None:
            return True, ""
        # A big bet or an all-in: remember it for the scorebug, and hand the
        # announcement back so the bot can post it in the channel.
        who = conn.execute("SELECT display_name FROM wallets WHERE user_id = ?",
                           (user_id,)).fetchone()["display_name"] or "Someone"
        what = _callout_subject(m, selection)
        conn.execute(
            """INSERT INTO callouts (race_id, bet_id, who, amount, what, all_in, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (race_id, cur.lastrowid, who, amount, what, kind == "allin", now),
        )
        return True, callout_text(who, amount, what, kind == "allin")


def _callout_subject(m, selection) -> str:
    if m["kind"] == "slate":
        return f"{selection[0]} to win"
    if m["kind"] == "prop":
        return f"{selection} for {label_of(m)[0].lower()}{label_of(m)[1:]}"
    return f"{selection} on the bonus"


def callout_text(who: str, amount: int, what: str, all_in: bool) -> str:
    if all_in:
        return f"{who} just went ALL IN: {amount:,} on {what}!"
    return f"{who} just put {amount:,} on {what}!"


def recent_callouts(race_id: int, seconds: int = 90) -> list[dict]:
    now = time.time()
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, who, amount, what, all_in, created_at FROM callouts
                WHERE race_id = ? AND created_at >= ? ORDER BY id""",
            (race_id, now - seconds),
        ).fetchall()
    return [{"id": r["id"], "who": r["who"], "amount": r["amount"], "what": r["what"],
             "all_in": bool(r["all_in"]), "age": round(now - r["created_at"], 1)} for r in rows]


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


_BET_WITH_MARKET = """
    SELECT b.*, m.kind, m.key, m.label, m.status AS market_status,
           m.result AS market_result, m.closes_at AS market_closes_at
      FROM bets b JOIN markets m ON m.id = b.market_id"""


def _still_open(row, now: float) -> bool:
    return (row["market_status"] == "open" and row["market_result"] is None
            and (row["market_closes_at"] is None or now < row["market_closes_at"]))


def _describe(row) -> dict:
    d = dict(row)
    d["selection"] = _decode(row["kind"], row["selection"])
    d["pick_text"] = " > ".join(d["selection"]) if row["kind"] == "slate" else d["selection"]
    d["label"] = label_of(row)
    return d


def cancellable_bets(race_id: int, user_id: int) -> list[dict]:
    """This person's bets that can still be taken back: unsettled, on a
    market that's still taking bets."""
    now = time.time()
    with connect() as conn:
        rows = conn.execute(
            _BET_WITH_MARKET + " WHERE b.race_id = ? AND b.user_id = ? AND b.settled = 0 ORDER BY b.id",
            (race_id, user_id),
        ).fetchall()
    return [_describe(r) for r in rows if _still_open(r, now)]


def cancel_bet(user_id: int, bet_id: int) -> tuple[bool, str, dict | None]:
    """Take a bet back and refund it in full, as if it was never placed.
    Only the owner can, and only while its market is still taking bets."""
    now = time.time()
    with connect() as conn:
        row = conn.execute(_BET_WITH_MARKET + " WHERE b.id = ? AND b.user_id = ?",
                           (bet_id, user_id)).fetchone()
        if row is None:
            return False, "That isn't one of your bets.", None
        if row["settled"]:
            return False, "That bet's already been settled.", None
        if not _still_open(row, now):
            return False, "Betting's locked on that one, so it stays.", None
        conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
        conn.execute("DELETE FROM callouts WHERE bet_id = ?", (bet_id,))
        adjust_balance(conn, user_id, row["amount"], staked=-row["amount"])
        return True, "", _describe(row)


def last_bet_id(user_id: int, market_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM bets WHERE user_id = ? AND market_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, market_id),
        ).fetchone()
    return row["id"] if row else None


def all_bets(race_id: int) -> list[dict]:
    """Every bet on a race, with who placed it, for /bets."""
    with connect() as conn:
        rows = conn.execute(
            _BET_WITH_MARKET.replace("FROM bets b", ", COALESCE(w.display_name, '?') AS who "
                                     "FROM bets b LEFT JOIN wallets w ON w.user_id = b.user_id")
            + " WHERE b.race_id = ? ORDER BY m.id, b.id",
            (race_id,),
        ).fetchall()
    return [dict(_describe(r), who=r["who"]) for r in rows]


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


def call_market_id(market_id: int, winner) -> dict:
    """Settle a prop or a bonus and pay it now. winner is one answer, a list
    of answers (a bonus with several winners), or None to void and refund."""
    m = market_by_id(market_id)
    if m is None:
        return {"error": "No such market."}
    if m["kind"] == "slate":
        return {"error": "Use /race result to enter the finishing order."}
    label = label_of(m)
    if m["result"] is not None:
        return {"error": f"{label} was already called."}
    winners = None if winner is None else ([winner] if isinstance(winner, str) else list(winner))
    for w in winners or []:
        if w not in options_of(m):
            return {"error": f"{w} isn't an option on {label}."}
    if m["kind"] == "bonus":
        return _settle_bonus(m, winners)
    if winners is not None and len(winners) != 1:
        return {"error": "A side bet has exactly one winner."}
    winner = None if winners is None else winners[0]

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
    auto_snapshot(r["week_label"])
    return {"finish": finish, "guesses": guesses}


KEEP_SNAPSHOTS = 20


def auto_snapshot(label: str) -> Path | None:
    """Save a copy of the whole database next to it (on the Railway volume),
    keeping the newest 20. Covers a bad night or a mistaken /reset without
    anyone remembering to run /backup. Never allowed to break a finish."""
    try:
        folder = DB_PATH.parent / "backups"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:40] or "race"
        dest = snapshot(folder / f"{time.strftime('%Y-%m-%d-%H%M%S')}-{safe}.db")
        for old in sorted(folder.glob("*.db"))[:-KEEP_SNAPSHOTS]:
            old.unlink(missing_ok=True)
        return dest
    except Exception:  # noqa: BLE001 - a failed backup must not stop the payout
        return None


TIE = "tie"


def settle_night(race_id: int, order: list[str], coins: str | None = None,
                 first_star: str | None = None) -> dict:
    """The whole post-game in one go: settle the tallied props from the
    panel counts, the coins prop, and every order guess, then close the night.

    Everything is checked before anything is paid, so a refusal leaves the
    race untouched and the crew can fix it and run it again."""
    r = race(race_id)
    if r is None or r["status"] == "settled":
        return {"error": "That race is already finished."}
    problem = check_order(race_id, order)
    if problem:
        return {"error": problem.replace("Guess the whole order", "Enter the whole order")}
    coins_market = market(race_id, "prop", "coins")
    needs_coins = coins_market is not None and coins_market["result"] is None
    if needs_coins:
        if not coins:
            return {"error": "Fill in coins: who had the most coins when the race ended. "
                             "Pick Tie if it was a tie."}
        if coins.lower() != TIE and coins not in options_of(coins_market):
            return {"error": f"{coins} isn't in this race."}
    star_market = market(race_id, "prop", "firststar")
    if first_star and star_market is not None and star_market["result"] is None \
            and first_star not in options_of(star_market):
        return {"error": f"{first_star} isn't in this race."}

    set_race_status(race_id, "locked")              # no late bets while paying out
    # Bonus bets nobody settled ("first to land on the bank" that never
    # happened) are refunded, so nobody loses points on something that didn't occur.
    refunded = []
    for m in bonus_markets(race_id, include_settled=False):
        call_market_id(m["id"], None)
        refunded.append(label_of(m))
    props = autograde(race_id)
    if needs_coins:
        props.append(call_market_id(coins_market["id"],
                                    None if coins.lower() == TIE else coins))
    if star_market is not None and star_market["result"] is None:
        # Usually settled from the control panel the moment it happens.
        props.append(call_market_id(star_market["id"], first_star or None))
    order_summary = settle_order(race_id, order)
    closed = finish_race(race_id)
    if "error" in closed:                           # a prop was left open by hand
        return closed
    return {"week": r["week_label"], "props": props, "order": order_summary,
            "refunded_bonuses": refunded, **closed}


def night_results(race_id: int, top: int = 5) -> dict | None:
    """What the winners reveal shows for a finished race: the final order,
    the props, the night's biggest winners across every bet they made, and
    anyone who called the whole order."""
    r = race(race_id)
    if r is None or r["status"] != "settled":
        return None
    slate = slate_market(race_id)
    finish = None
    if slate and slate["result"] and slate["result"] != "VOID":
        finish = json.loads(slate["result"])
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.user_id, w.display_name, m.kind, b.selection, b.amount,
                      COALESCE(b.payout, 0) AS payout
                 FROM bets b JOIN markets m ON m.id = b.market_id
                 LEFT JOIN wallets w ON w.user_id = b.user_id
                WHERE b.race_id = ?""",
            (race_id,),
        ).fetchall()
    people: dict[int, dict] = {}
    perfect: list[str] = []
    for x in rows:
        who = people.setdefault(x["user_id"], {"name": x["display_name"] or "Someone",
                                               "staked": 0, "returned": 0})
        who["staked"] += x["amount"]
        who["returned"] += x["payout"]
        if (x["kind"] == "slate" and finish
                and economy.positions_right(json.loads(x["selection"]), finish) == len(finish)
                and who["name"] not in perfect):
            perfect.append(who["name"])
    winners = sorted((dict(p, profit=p["returned"] - p["staked"]) for p in people.values()
                      if p["returned"] > p["staked"]),
                     key=lambda p: (-p["profit"], p["name"]))[:top]
    props = []
    for m in markets(race_id, ("prop",)):
        props.append({"label": label_of(m),
                      "winner": None if m["result"] in (None, "VOID") else m["result"]})
    return {"race_id": race_id, "week": r["week_label"], "game": r["game"], "finish": finish,
            "winners": winners, "perfect": perfect, "props": props,
            "players": len(people)}


# ----------------------------------------------------------------- reset


def reset_economy(guild_id: int, name: str) -> dict:
    """/reset: archive the current table into the hall of fame, put every
    wallet back to the starting balance, and open a new season under name.

    Wallets are reset even when no season was running (say, points were only
    ever gifted), because that's what the host was promised when confirming."""
    out = start_season(guild_id, name)
    if out["closed"] is None:
        with connect() as conn:
            conn.execute(
                "UPDATE wallets SET balance = ?, staked = 0, returned = 0, adjusted = 0, "
                "last_stipend = ''",
                (economy.STARTING_BALANCE,),
            )
    return out


# ------------------------------------------------------------- form guide

FORM_LENGTH = 5


def form(names: list[str], mode: str, guild_id: int | None = None) -> dict[str, dict]:
    """Each runner's record in past finished races of the same mode.

    recent is their last few finishing places, latest first. Names match
    ignoring capitals, so "mario" last week and "Mario" tonight are one
    runner. Voided results don't count."""
    wanted = {n.lower(): n for n in names}
    out = {n: {"recent": [], "starts": 0, "wins": 0, "total": 0} for n in names}
    sql = """SELECT m.result FROM markets m JOIN races r ON r.id = m.race_id
              WHERE m.kind = 'slate' AND r.status = 'settled' AND r.mode = ?
                AND m.result IS NOT NULL AND m.result != 'VOID'"""
    args: list = [mode]
    if guild_id:
        sql += " AND r.guild_id = ?"
        args.append(guild_id)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY r.settled_at DESC, r.id DESC", args).fetchall()
    for row in rows:
        for place, name in enumerate(json.loads(row["result"]), start=1):
            runner = wanted.get(name.lower())
            if runner is None:
                continue
            rec = out[runner]
            rec["starts"] += 1
            rec["total"] += place
            rec["wins"] += place == 1
            if len(rec["recent"]) < FORM_LENGTH:
                rec["recent"].append(place)
    for rec in out.values():
        rec["average"] = round(rec["total"] / rec["starts"], 1) if rec["starts"] else None
    return out


# ----------------------------------------------------------- bonus markets


def open_bonus(
    race_id: int, question: str, options: list[str], seconds: int | None,
    multiplier: int | None = None, bonus_type: str = "custom", max_picks: int | None = None,
) -> int:
    """A bonus bet with its own clock. seconds=None keeps it open until
    betting locks for the race, for "who'll be first to..." bets set up at
    the start of the show. Several can run at once."""
    now = time.time()
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM markets WHERE race_id = ? AND kind = 'bonus'", (race_id,)
        ).fetchone()["n"]
        priced = [o for o in options if o != economy.DRAW]
        cur = conn.execute(
            """INSERT INTO markets
                 (race_id, kind, key, status, label, options, multiplier, opened_at, closes_at,
                  bonus_type, max_picks)
               VALUES (?, 'bonus', ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
            (race_id, f"b{n + 1}", question, json.dumps(options),
             multiplier or economy.bonus_multiplier(len(priced)), now,
             None if seconds is None else now + seconds, bonus_type, max_picks),
        )
        return cur.lastrowid


def bonus_price(m, selection: str) -> int:
    if m["bonus_type"] == "minigame" and selection == economy.DRAW:
        return economy.DRAW_MULTIPLIER
    return m["multiplier"]


def place_comeback(market_id: int, user_id: int, display_name: str, selection: str) -> tuple[bool, str]:
    """A free pick on a minigame bet for someone with no points left.
    One per person per minigame, worth COMEBACK_PRIZE if it lands."""
    wallet(user_id, display_name)
    with connect() as conn:
        m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or not bonus_taking_bets(m):
            return False, "That bonus is closed."
        if m["bonus_type"] != "minigame":
            return False, "Free picks are only on minigame bets."
        balance = conn.execute("SELECT balance FROM wallets WHERE user_id = ?",
                               (user_id,)).fetchone()["balance"]
        if balance >= economy.MIN_WAGER:
            return False, "You still have points, so this one's a normal bet."
        if conn.execute("SELECT 1 FROM bets WHERE market_id = ? AND user_id = ? AND comeback = 1",
                        (market_id, user_id)).fetchone():
            return False, "You've already got your free pick on this one."
        conn.execute(
            """INSERT INTO bets (race_id, market_id, user_id, selection, amount, placed_at, comeback)
               VALUES (?,?,?,?,0,?,1)""",
            (m["race_id"], market_id, user_id, selection, time.time()),
        )
        return True, ""


def close_bonus(market_id: int) -> bool:
    """Stop taking bets now, before the timer runs out."""
    with connect() as conn:
        return conn.execute(
            """UPDATE markets SET status = 'locked', closes_at = MIN(COALESCE(closes_at, ?), ?)
                WHERE id = ? AND kind = 'bonus' AND result IS NULL""",
            (time.time(), time.time(), market_id),
        ).rowcount == 1


def bonus_backers(market_id: int) -> dict[str, list[tuple[str, int, bool]]]:
    """Who backed each answer: (name, stake, free pick) per answer."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.selection, b.amount, b.comeback, COALESCE(w.display_name, '?') AS name
                 FROM bets b LEFT JOIN wallets w ON w.user_id = b.user_id
                WHERE b.market_id = ? ORDER BY b.id""",
            (market_id,),
        ).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["selection"], []).append((r["name"], r["amount"], bool(r["comeback"])))
    return out


def _settle_bonus(m, winners: list[str] | None) -> dict:
    """Pay a bonus. Several winners are allowed (a 2 v 2 minigame); a draw
    pays 8x on a minigame bet; a free comeback pick pays COMEBACK_PRIZE."""
    label = label_of(m)
    result = "VOID" if winners is None else " + ".join(winners)
    with connect() as conn:
        conn.execute(
            """UPDATE markets SET result = ?, status = 'settled', called_at = ?,
                                  closes_at = COALESCE(closes_at, ?) WHERE id = ?""",
            (result, time.time(), time.time(), m["id"]),
        )
        bets = conn.execute(
            "SELECT id, selection, amount, comeback FROM bets WHERE market_id = ? AND settled = 0",
            (m["id"],),
        ).fetchall()
        payouts = {}
        for b in bets:
            if winners is None:
                payouts[b["id"]] = b["amount"]                   # void: stake back
            elif b["selection"] in winners:
                payouts[b["id"]] = (economy.COMEBACK_PRIZE if b["comeback"]
                                    else b["amount"] * bonus_price(m, b["selection"]))
            else:
                payouts[b["id"]] = 0
        _apply(conn, payouts)
    return {"label": label, "winner": None if winners is None else " and ".join(winners),
            "winners_list": winners or [], "kind": "bonus", "voided": winners is None,
            "tickets": len(bets),
            "winners": 0 if winners is None else sum(1 for v in payouts.values() if v),
            "paid": sum(payouts.values()), "multiplier": m["multiplier"]}


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
        after = max(1, min(r["total_turns"], r["turn"] + delta))   # turn 1 is the first
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
            conn.execute("UPDATE races SET turn = MAX(1, turn - ?) WHERE id = ?",
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
