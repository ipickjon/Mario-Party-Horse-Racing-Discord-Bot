"""Everything that only matters once the bot is on Railway."""

import asyncio
import json
import logging
import re
import sqlite3
import types
from pathlib import Path

import discord
import pytest
from fastapi.testclient import TestClient

from mpr import bot as B
from mpr import db, overlay
from tests.harness import Interaction, build_bot, run

ROOT = Path(__file__).resolve().parent.parent
RAILWAY_ENV = ("MPR_DB_PATH", "RAILWAY_VOLUME_MOUNT_PATH", "PORT", "MPR_OVERLAY_PORT",
               "MPR_OVERLAY_HOST", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PROJECT_ID",
               "RAILWAY_ENVIRONMENT_NAME")


@pytest.fixture
def clean_env(monkeypatch):
    for var in RAILWAY_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# --- where the season is stored --------------------------------------------------

def test_database_goes_on_the_railway_volume_when_there_is_one(clean_env):
    assert db.resolve_db_path() == ROOT / "data" / "horserace.db"
    clean_env.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
    assert db.resolve_db_path() == Path("/app/data/horserace.db")
    clean_env.setenv("MPR_DB_PATH", "/tmp/scratch.db")
    assert db.resolve_db_path() == Path("/tmp/scratch.db")


def test_warns_when_running_on_railway_without_a_volume(clean_env):
    assert not B.on_railway_without_volume()                 # local: fine
    clean_env.setenv("RAILWAY_PROJECT_ID", "p")
    assert B.on_railway_without_volume()                     # the season-wiping mistake
    clean_env.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
    assert not B.on_railway_without_volume()


# --- how OBS reaches the overlay ----------------------------------------------------

def test_overlay_listens_locally_or_on_railways_port(clean_env):
    assert overlay.listen_address() == ("127.0.0.1", 8730)
    clean_env.setenv("PORT", "8080")
    assert overlay.listen_address() == ("0.0.0.0", 8080)


def test_the_logged_obs_url_uses_the_public_domain_and_key(clean_env, monkeypatch):
    clean_env.setenv("RAILWAY_PUBLIC_DOMAIN", "mpr-production.up.railway.app")
    monkeypatch.setattr(overlay, "OVERLAY_KEY", "s3cret")
    assert overlay.public_url("tote") == "https://mpr-production.up.railway.app/tote?key=s3cret"


def test_overlay_key_locks_everything_but_the_health_check(fresh_db, monkeypatch):
    monkeypatch.setattr(overlay, "OVERLAY_KEY", "s3cret")
    client = TestClient(overlay.app)
    assert client.get("/healthz").status_code == 200         # Railway's deploy check
    assert client.get("/state.json").status_code == 403
    assert client.get("/state.json?key=wrong").status_code == 403
    assert client.get("/state.json?key=s3cret").status_code == 200
    for page in ("tote", "bug", "bonus", "casters", "ads", "standings"):
        assert client.get(f"/{page}").status_code == 403
        assert client.get(f"/{page}?key=s3cret").status_code == 200
    assert client.get("/docs").status_code in (403, 404)     # no auto docs in public


def test_pages_pass_their_key_on_to_their_data_requests():
    for page in ("tote", "bug", "bonus", "casters", "ads", "standings"):
        html = (ROOT / "mpr" / "web" / f"{page}.html").read_text()
        fetches = re.findall(r'fetch\("[^"]*"[^)]*', html)
        assert fetches and all("location.search" in f for f in fetches), page


# --- Discord-side safety ------------------------------------------------------------

def test_bot_ignores_servers_other_than_its_home(fresh_db, monkeypatch):
    monkeypatch.setattr(B, "GUILD_ID", 42)

    async def go():
        bot = await build_bot()
        home, stranger = Interaction(guild_id=42), Interaction(guild_id=777)
        assert await bot.tree.interaction_check(home)
        assert not await bot.tree.interaction_check(stranger)
        assert "only runs in its home server" in stranger.text

    asyncio.run(go())


def test_wrong_server_id_logs_a_fix_instead_of_crashing(fresh_db, monkeypatch, caplog):
    monkeypatch.setattr(B, "GUILD_ID", 4242)
    monkeypatch.setattr(overlay, "listen_address", lambda: ("127.0.0.1", 0))

    async def go():
        bot = B.HorseRace()

        async def refuse(*, guild=None):
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"),
                                    {"code": 50001, "message": "Missing Access"})
        monkeypatch.setattr(bot.tree, "sync", refuse)
        await bot.setup_hook()                                   # must not raise
        for t in list(bot._background):
            t.cancel()
        await asyncio.gather(*bot._background, return_exceptions=True)

    with caplog.at_level(logging.ERROR, logger="mpr"):
        asyncio.run(go())
    assert any("Check MPR_GUILD_ID" in r.getMessage() for r in caplog.records)


def test_missing_or_bad_token_explains_the_fix(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="DISCORD_TOKEN isn't set"):
        B.main()
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")

    def reject(self, token, **kw):
        raise discord.LoginFailure("Improper token")
    monkeypatch.setattr(B.HorseRace, "run", reject)
    with pytest.raises(SystemExit, match="Reset the token"):
        B.main()


# --- /backup ---------------------------------------------------------------------

def test_backup_is_host_only_and_is_a_real_copy_of_the_season(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "race create", Interaction(user_id=10, staff=True), week="W1",
                  runners="Mario, Luigi, Peach, Yoshi")
        db.wallet(20, "ana")
        crew = await run(bot, "backup", Interaction(user_id=10, staff=True))
        assert "Only the server host" in crew.text and crew.sent[0]["file"] is None
        host = await run(bot, "backup", Interaction(user_id=99, host=True))
        return host.sent[0]

    sent = asyncio.run(go())
    assert sent["ephemeral"] and sent["file"] is not None
    copy = sqlite3.connect(sent["file"].fp.name)
    try:
        names = {r[0] for r in copy.execute("SELECT display_name FROM wallets")}
        races = copy.execute("SELECT week_label FROM races").fetchall()
    finally:
        copy.close()
    assert "ana" in names and races == [("W1",)]


# --- the deploy files themselves ------------------------------------------------------

def test_railway_config_uses_only_settings_railway_knows():
    """Railway ignores misspelled settings silently, so check the names against
    its published schema (transcribed from railway.com/railway.schema.json)."""
    known_build = {"builder", "watchPatterns", "buildCommand", "dockerfilePath",
                   "nixpacksConfigPath", "nixpacksPlan", "nixpacksVersion", "railpackVersion"}
    known_deploy = {"startCommand", "preDeployCommand", "preDeployTimeoutSeconds", "numReplicas",
                    "healthcheckPath", "healthcheckTimeout", "sleepApplication", "runtime",
                    "registryCredentials", "restartPolicyType", "restartPolicyMaxRetries",
                    "cronSchedule", "region", "multiRegionConfig", "limitOverride",
                    "requiredMountPath", "overlapSeconds", "drainingSeconds", "ipv6EgressEnabled"}
    cfg = json.loads((ROOT / "railway.json").read_text())
    assert set(cfg["build"]) <= known_build and set(cfg["deploy"]) <= known_deploy
    assert cfg["build"]["builder"] in {"RAILPACK", "NIXPACKS"}
    assert cfg["deploy"]["restartPolicyType"] in {"ON_FAILURE", "ALWAYS", "NEVER"}
    assert cfg["deploy"]["numReplicas"] == 1                  # one bot, one SQLite writer
    assert cfg["deploy"]["startCommand"] == "python -m mpr.bot"
    assert cfg["deploy"]["healthcheckPath"] in {r.path for r in overlay.app.routes}


def test_pinned_versions_are_the_ones_tested():
    from importlib.metadata import version
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        if "==" in line and not line.startswith("#"):
            name, want = line.split("==")
            assert version(name) == want, name
    assert (ROOT / ".python-version").read_text().strip() == "3.12"


def test_the_season_file_is_never_committed_to_git():
    ignored = (ROOT / ".gitignore").read_text()
    assert "data/" in ignored and "*.db" in ignored and ".env" in ignored
