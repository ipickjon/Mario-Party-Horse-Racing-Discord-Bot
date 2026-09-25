"""Mario Party Horse Racing — Discord bot.

Players:  /bet  /payouts  /wallet  /mybets  /board  /leaderboard  /season ...
Crew:     /race ...  /show ...  /bonus ...  /panel  /tally  /railmoney  /feature

Crew commands need the role named in MPR_STAFF_ROLE (default "Race Staff"),
or Manage Server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from . import db, economy, overlay

STAFF_ROLE = os.getenv("MPR_STAFF_ROLE", "Race Staff")
CURRENCY = os.getenv("MPR_CURRENCY", "points")
GUILD_ID = int(os.getenv("MPR_GUILD_ID", "0"))
DISCLAIMER = "No real money. All bets use fake Discord points."
DEFAULT_BONUS_STAKE = 100
log = logging.getLogger("mpr")

PROP_CHOICES = [
    app_commands.Choice(name="Most minigames won", value="prop:minigames"),
    app_commands.Choice(name="Most coins at the end", value="prop:coins"),
    app_commands.Choice(name="Most ? tiles stepped on", value="prop:qtiles"),
]
VOID_CHOICES = [app_commands.Choice(name="Finishing order", value="slate:order")] + PROP_CHOICES

# /bet and /race result take the order as one option per place, so a Mario
# Kart field of 12 works the same way as a Mario Party field of 4.
PLACES = ["first", "second", "third", "fourth", "fifth", "sixth",
          "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth"]
PLACE_HELP = {p: f"Who finishes {db.ordinal(i)}" for i, p in enumerate(PLACES, start=1)}


def gather_order(**places) -> list[str]:
    """The filled-in places, in order, stopping at the first gap."""
    order = []
    for p in PLACES:
        value = places.get(p)
        if not value:
            break
        order.append(value.strip())
    return order


TALLY_CHOICES = [
    app_commands.Choice(name="? tile", value="qtiles"),
    app_commands.Choice(name="Minigame win", value="minigames"),
]


class Refusal(app_commands.AppCommandError):
    """A polite no. Shown to the user as-is."""


def split_market(value: str) -> tuple[str, str]:
    kind, key = value.split(":", 1)
    return kind, key


def is_staff(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.manage_guild:
        return True
    return any(getattr(r, "name", None) == STAFF_ROLE for r in getattr(user, "roles", []))


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        raise app_commands.CheckFailure(f"That one's for the crew. You need the {STAFF_ROLE} role.")
    return app_commands.check(predicate)


def host_only():
    """For the server host: anyone with Manage Server."""
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        raise app_commands.CheckFailure("Only the server host can reset the economy.")
    return app_commands.check(predicate)


def reset_embed(out: dict, name: str) -> discord.Embed:
    podium = "\n".join(f"`{i}.` **{c['display_name']}** — {c['balance']:,}"
                        for i, c in enumerate(out["champions"], start=1))
    title = f"{out['closed']} is over" if out["closed"] else f"{name} is open"
    lead = f"{podium}\n\n" if podium else ""
    embed = discord.Embed(
        title=title,
        description=(f"{lead}**{name}** starts now. Every wallet is back to "
                     f"{economy.STARTING_BALANCE:,} {CURRENCY}."
                     + ("\nLast season's table is saved in `/season hall`." if out["closed"] else "")),
        color=0xFFB114)
    embed.set_footer(text=DISCLAIMER)
    return embed


class ConfirmReset(discord.ui.View):
    """A second click before wiping every wallet."""

    def __init__(self, invoker_id: int, guild_id: int, name: str):
        super().__init__(timeout=120)
        self.invoker_id, self.guild_id, self.name = invoker_id, guild_id, name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message(
            "Only whoever ran /reset can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Reset everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if db.active_race(self.guild_id):
            await interaction.response.edit_message(
                content="A race started since you asked. Finish it, then reset.", view=None)
            return
        out = db.reset_economy(self.guild_id, self.name)
        self.stop()
        await interaction.response.edit_message(
            content=None, embed=reset_embed(out, self.name), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Reset cancelled. Nothing changed.",
                                                view=None)


def require_race(guild_id: int):
    r = db.active_race(guild_id)
    if r is None:
        raise Refusal("No race is set up right now.")
    return r


# ------------------------------------------------------------ text builders


def ladder_line(n: int = 4) -> str:
    tiers = economy.ladder_table(n)
    if len(tiers) > 4:
        return f"each place you get right multiplies your stake by one more, up to {n}x"
    return ", ".join(f"{k} right pays {m}x" for k, m, _ in tiers)


def board_embed(race) -> discord.Embed:
    ents = db.entrants(race["id"])
    n = len(ents)
    embed = discord.Embed(
        title=f"{race['week_label']} — {race['game']}",
        description=(f"Betting is **{race['status']}**.\n"
                     f"Finishing order: {ladder_line(n)}."
                     + (f"\nProps pay {economy.PROP_MULTIPLIER}x."
                        if db.markets(race["id"], ("prop",)) else "")),
        color=0x1E7A46,
    )
    views = db.slate_views(race["id"])
    names = [e["name"] for e in ents]
    for place, money in list(enumerate(views, start=1))[:4]:
        staked = sum(money.values())
        top = sorted(names, key=lambda x: -money.get(x, 0))[:4]
        lines = [f"`{x:<10} {round(money.get(x, 0) / staked * 100) if staked else 0:>3}%`"
                 for x in top] if staked else ["No guesses yet."]
        embed.add_field(name=f"Picked to finish {db.ordinal(place)}", value="\n".join(lines))
    for m in db.markets(race["id"], ("prop",)):
        totals = db.market_totals(m["id"])
        staked = sum(totals.values())
        lines = [f"`{x:<10} {totals.get(x, 0):>5,}`" for x in names if totals.get(x, 0)]
        embed.add_field(name=f"{db.label_of(m)} — {staked:,} down",
                        value="\n".join(lines) or "No bets yet.", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def has_props(race_id: int) -> bool:
    return bool(db.markets(race_id, ("prop",)))


def panel_text(race_id: int) -> str:
    """What the crew's control panel shows: turn, both tallies, who leads."""
    r = db.race(race_id)
    counts = db.tallies(race_id)
    lead_q = economy.tally_leader({n: c["qtiles"] for n, c in counts.items()})
    lead_m = economy.tally_leader({n: c["minigames"] for n, c in counts.items()})
    rows = []
    for name, c in counts.items():
        q = f"{c['qtiles']}{'*' if name == lead_q else ' '}"
        m = f"{c['minigames']}{'*' if name == lead_m else ' '}"
        rows.append(f"{name:<10} {q:>4} {m:>6}")
    last5 = r["total_turns"] - r["turn"] < 5 and r["turn"] > 0
    turn = f"Turn {r['turn']} of {r['total_turns']}" + ("  — last five turns" if last5 else "")
    return (f"**{r['week_label']} control panel** — {turn}\n"
            "```\n" + f"{'':<10} {'? tiles':>4} {'games':>6}\n" + "\n".join(rows) + "\n```"
            "Tap a character each time it happens. Undo reverses the last tap. "
            "\\* marks the outright leader.")


def rundown_text(r) -> str:
    state = db.show_state(r)
    if state is None:
        return "No show running."
    lines = []
    for i, seg in enumerate(state["segments"]):
        mark = "▶" if i == state["index"] else ("✓" if i < state["index"] else " ")
        extra = f"  ({seg['action']}s betting)" if seg.get("action") else ""
        lines.append(f"`{mark}` {seg['name']} — {seg['minutes']} min{extra}")
    total = sum(s["minutes"] for s in state["segments"])
    return "\n".join(lines) + f"\n\nPlanned total {total // 60}h {total % 60:02d}m."


def call_text(summary: dict) -> str:
    if "error" in summary:
        return summary["error"]
    label = summary["label"]
    if summary["voided"]:
        return f"**{label}** voided. Every stake on it refunded."
    if summary["kind"] == "slate":
        order = ", ".join(f"{db.ordinal(i)} {x}" for i, x in enumerate(summary["finish"], 1))
        cashed = [r for r in summary["results"] if r["returned"] > 0]
        lines = [f"**{r['name']}** {r['right']} right, {r['staked']:,} became {r['returned']:,}"
                 for r in cashed[:8]]
        body = "\n".join(lines) if lines else "Nobody got a single place right."
        return (f"**Final order:** {order}\n{len(cashed)} of {summary['tickets']} "
                f"guesses paid out.\n{body}")
    return (f"**{label}** — {summary['winner']}, paying {summary['multiplier']}x.\n"
            f"{summary['winners']} of {summary['tickets']} tickets cashed, "
            f"{summary['paid']:,} paid out.")


def bonus_embed(m) -> discord.Embed:
    options = db.options_of(m)
    totals = db.market_totals(m["id"])
    staked = sum(totals.values())
    if m["result"] is not None:
        state = "voided, stakes refunded" if m["result"] == "VOID" else f"**{m['result']}** takes it"
    elif m["status"] == "open" and time.time() < (m["closes_at"] or 0):
        state = f"closes <t:{int(m['closes_at'])}:R>"
    else:
        state = "locked"
    lines = [f"`{o:<12}` {totals.get(o, 0):>6,} in" for o in options]
    embed = discord.Embed(
        title=f"Bonus: {m['label']}",
        description=(f"Pays **{m['multiplier']}x**, {state}.\n"
                     f"Tap an answer to bet. Default stake {DEFAULT_BONUS_STAKE}.\n\n"
                     + "\n".join(lines) + f"\n\n{staked:,} {CURRENCY} down"),
        color=0xFFA81E,
    )
    embed.set_footer(text=DISCLAIMER)
    return embed


# ------------------------------------------------------ plain-logic handlers
# Buttons and modals call these. Kept free of discord objects so they can be
# tested directly.


def apply_tally(race_id: int, what: str, arg: int, guild_id: int | None = None) -> str:
    """One tap on the control panel. Returns a short note, or '' on success."""
    r = db.race(race_id)
    if r is None or r["status"] == "settled":
        return "This panel is for a finished race. Run /panel again."
    if guild_id is not None:
        active = db.active_race(guild_id)
        if active is None or active["id"] != race_id:
            return "This panel is for an old race. Run /panel again."
    if what in ("qt", "mg"):
        e = db.entrant_by_slot(race_id, arg)
        db.bump_tally(race_id, e["name"], 1, "qtiles" if what == "qt" else "minigames")
    elif what == "tn":
        db.bump_turn(race_id, arg)
    elif what == "ud":
        if db.undo_last(race_id) is None:
            return "Nothing to undo."
    return ""


def submit_bonus_stake(user_id: int, display_name: str, market_id: int,
                       option_index: int, raw_amount: str) -> str:
    """A viewer backing an answer in a bonus market."""
    m = db.market_by_id(market_id)
    if m is None:
        return "That bonus is gone."
    options = db.options_of(m)
    if not 0 <= option_index < len(options):
        return "That answer isn't on this bonus."
    try:
        amount = int(str(raw_amount).replace(",", "").strip())
    except ValueError:
        return "Whole points only."
    db.wallet(user_id, display_name)
    pick = options[option_index]
    ok, why = db.place_bet(m["race_id"], market_id, user_id, pick, amount)
    if not ok:
        return why
    left = db.wallet(user_id)["balance"]
    return (f"{amount:,} on **{pick}**. Pays {m['multiplier']}x if right. "
            f"{left:,} {CURRENCY} left.")


# ----------------------------------------------------------------- buttons


class TallyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"mpr:t:(?P<race>\d+):(?P<what>qt|mg|tn|ud):(?P<arg>-?\d+)"):
    """Control panel button. The custom id carries everything it needs, so
    the panel keeps working after the bot restarts."""

    def __init__(self, race_id: int, what: str, arg: int,
                 label: str = "·", style=discord.ButtonStyle.secondary, row: int | None = None):
        super().__init__(discord.ui.Button(
            label=label, style=style, row=row, custom_id=f"mpr:t:{race_id}:{what}:{arg}"))
        self.race_id, self.what, self.arg = race_id, what, arg

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["race"]), match["what"], int(match["arg"]),
                   item.label, item.style, item.row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Crew only.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        note = apply_tally(self.race_id, self.what, self.arg, interaction.guild_id)
        if note.startswith("This panel"):
            await interaction.response.send_message(note, ephemeral=True)
            return
        text = panel_text(self.race_id) + (f"\n_{note}_" if note else "")
        await interaction.response.edit_message(content=text)


def panel_view(race_id: int) -> discord.ui.View:
    """Tally buttons for a Mario Party race; turn buttons for anything.
    Discord allows five buttons a row, so tallies need a field of five or fewer."""
    view = discord.ui.View(timeout=None)
    ents = db.entrants(race_id)
    if has_props(race_id) and len(ents) <= 5:
        for e in ents:
            view.add_item(TallyButton(race_id, "qt", e["slot"], f"? {e['name']}",
                                      discord.ButtonStyle.primary, row=0))
        for e in ents:
            view.add_item(TallyButton(race_id, "mg", e["slot"], f"Won: {e['name']}",
                                      discord.ButtonStyle.success, row=1))
    view.add_item(TallyButton(race_id, "tn", 1, "Next turn", discord.ButtonStyle.secondary, row=2))
    view.add_item(TallyButton(race_id, "tn", -1, "Turn back", discord.ButtonStyle.secondary, row=2))
    view.add_item(TallyButton(race_id, "ud", 0, "Undo last", discord.ButtonStyle.danger, row=2))
    return view


class BonusStakeModal(discord.ui.Modal):
    def __init__(self, market_id: int, option_index: int, option: str):
        super().__init__(title=f"Back {option}"[:45])
        self.market_id, self.option_index = market_id, option_index
        self.amount = discord.ui.TextInput(
            label=f"How many {CURRENCY}?", default=str(DEFAULT_BONUS_STAKE), max_length=7)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        text = submit_bonus_stake(interaction.user.id, interaction.user.display_name,
                                  self.market_id, self.option_index, self.amount.value)
        await interaction.response.send_message(text, ephemeral=True)


class BonusButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"mpr:b:(?P<market>\d+):(?P<idx>\d+)"):
    def __init__(self, market_id: int, idx: int, label: str = "·"):
        super().__init__(discord.ui.Button(
            label=label[:80], style=discord.ButtonStyle.primary,
            custom_id=f"mpr:b:{market_id}:{idx}", row=idx // 5))
        self.market_id, self.idx = market_id, idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["market"]), int(match["idx"]), item.label)

    async def callback(self, interaction: discord.Interaction):
        m = db.market_by_id(self.market_id)
        if m is None or m["result"] is not None or m["status"] != "open" \
                or time.time() >= (m["closes_at"] or 0):
            await interaction.response.send_message("That bonus is closed.", ephemeral=True)
            return
        option = db.options_of(m)[self.idx]
        await interaction.response.send_modal(BonusStakeModal(self.market_id, self.idx, option))


def bonus_view(market_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i, option in enumerate(db.options_of(db.market_by_id(market_id))):
        item = BonusButton(market_id, i, option)
        item.item.disabled = disabled
        view.add_item(item)
    return view


# --------------------------------------------------------------------- bot


def on_railway_without_volume() -> bool:
    on_railway = any(os.getenv(v) for v in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_NAME"))
    return on_railway and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH") and not os.getenv("MPR_DB_PATH")


class HomeServerTree(app_commands.CommandTree):
    """With MPR_GUILD_ID set, answer only in that server. Wallets are shared
    across every server the bot is in, so a stranger who added the bot to
    their own server must not be able to run races and farm points there."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not GUILD_ID or interaction.guild_id == GUILD_ID:
            return True
        await interaction.response.send_message(
            "This bot only runs in its home server.", ephemeral=True)
        return False


class HorseRace(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!mpr ", intents=discord.Intents.default(),
                         tree_cls=HomeServerTree)
        self._background: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def setup_hook(self):
        db.init()
        db.lock_expired_bonuses()
        self.add_dynamic_items(TallyButton, BonusButton)
        for cog in (Betting(self), Broadcast(self), Show(self), Bonus(self)):
            await self.add_cog(cog)
        self.spawn(overlay.serve())
        try:
            if GUILD_ID:
                # Guild sync lands instantly. Global sync can take a while to show up.
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                log.warning("MPR_GUILD_ID isn't set. Commands will register globally, "
                            "which can take a while to show up.")
                await self.tree.sync()
        except discord.HTTPException as error:
            # Almost always a wrong server id, or the bot not invited yet.
            log.error("Couldn't register slash commands in server %s (%s). Check "
                      "MPR_GUILD_ID, and that the bot has been added to that server "
                      "using the install link.", GUILD_ID, error)

    async def on_ready(self):
        log.info("Logged in as %s. In %d server(s).", self.user, len(self.guilds))
        if GUILD_ID and all(g.id != GUILD_ID for g in self.guilds):
            log.error("The bot isn't in server %s. Use the install link from the "
                      "Discord developer portal to add it.", GUILD_ID)
        log.info("Overlay ready. OBS source for the board: %s", overlay.public_url("tote"))
        log.info("Database: %s", db.DB_PATH)
        if on_railway_without_volume():
            log.error("NO VOLUME ATTACHED. The season is being stored inside the container "
                      "and will be wiped on the next deploy. Add a volume to this service "
                      "(mount path /app/data) before anyone places a bet.")


async def entrant_options(interaction: discord.Interaction, current: str):
    r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
    if r is None:
        return []
    return [app_commands.Choice(name=e["name"], value=e["name"])
            for e in db.entrants(r["id"]) if current.lower() in e["name"].lower()][:25]


class Betting(commands.Cog):
    def __init__(self, bot: HorseRace):
        self.bot = bot

    @app_commands.command(description="Guess the finishing order and put points on it.")
    @app_commands.describe(amount="How many points to stake on this guess", **PLACE_HELP)
    @app_commands.autocomplete(**{p: entrant_options for p in PLACES})
    async def bet(self, interaction: discord.Interaction, amount: int,
                  first: str, second: str, third: str | None = None, fourth: str | None = None,
                  fifth: str | None = None, sixth: str | None = None,
                  seventh: str | None = None, eighth: str | None = None,
                  ninth: str | None = None, tenth: str | None = None,
                  eleventh: str | None = None, twelfth: str | None = None):
        r = require_race(interaction.guild_id)
        db.wallet(interaction.user.id, interaction.user.display_name)
        order = gather_order(first=first, second=second, third=third, fourth=fourth,
                             fifth=fifth, sixth=sixth, seventh=seventh, eighth=eighth,
                             ninth=ninth, tenth=tenth, eleventh=eleventh, twelfth=twelfth)
        ok, reason = db.place_slate(r["id"], interaction.user.id, order, amount)
        if not ok:
            raise Refusal(reason)
        n = len(order)
        balance = db.wallet(interaction.user.id)["balance"]
        await interaction.response.send_message(
            f"{amount:,} on **{' > '.join(order)}**.\n"
            f"Get all {n} right and it comes back as {amount * economy.slate_multiplier(n):,}. "
            f"{ladder_line(n).capitalize()}.\nYou have {balance:,} {CURRENCY} left.",
            ephemeral=True)

    @app_commands.command(description="Side bet on a prop: minigames, coins, or ? tiles.")
    @app_commands.describe(market="Which prop", pick="Which character",
                           amount=f"How many {CURRENCY} to stake")
    @app_commands.choices(market=PROP_CHOICES)
    @app_commands.autocomplete(pick=entrant_options)
    async def prop(self, interaction: discord.Interaction,
                   market: app_commands.Choice[str], pick: str, amount: int):
        r = require_race(interaction.guild_id)
        db.wallet(interaction.user.id, interaction.user.display_name)
        _, key = split_market(market.value)
        m = db.market(r["id"], "prop", key)
        if m is None:
            raise Refusal("Tonight's race has no props.")
        names = db.options_of(m)
        if pick not in names:
            raise Refusal(f"{pick} isn't in this race. Tonight: {', '.join(names)}.")
        ok, reason = db.place_bet(r["id"], m["id"], interaction.user.id, pick, amount)
        if not ok:
            raise Refusal(reason)
        balance = db.wallet(interaction.user.id)["balance"]
        await interaction.response.send_message(
            f"{amount:,} on **{pick}** — {market.name}. Pays {economy.PROP_MULTIPLIER}x "
            f"if you're right.\nYou have {balance:,} {CURRENCY} left.", ephemeral=True)

    @app_commands.command(description="What everything pays.")
    async def payouts(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        n = len(db.entrants(r["id"])) if r else 4
        rows = "\n".join(f"`{k} right`  stake × **{m}**  — a random guess does this "
                         f"{c:.0%} of the time" for k, m, c in economy.ladder_table(n)[:6])
        example = 100 if economy.STARTING_BALANCE >= 100 else economy.STARTING_BALANCE
        embed = discord.Embed(
            title="How it pays",
            description=(
                f"**Finishing order.** Guess where all {n} finish and put points on it with "
                f"`/bet`. You get your stake back multiplied by how many places you got right. "
                f"Spend {example} and get all {n} right: {example * economy.slate_multiplier(n):,}. "
                f"Get none right and the stake is gone.\n\n{rows}\n\n"
                f"**Props.** `/prop` on most minigames, most coins, or most ? tiles. "
                f"Pays {economy.PROP_MULTIPLIER}x. Minigames goes to whoever won the most "
                "minigames. Coins goes to whoever has the most coins when the race ends. "
                "The game's own bonus stars don't decide these. A tie refunds the bet.\n\n"
                "**Bonus questions** pop up during the race. Tap an answer on the post.\n\n"
                f"Everyone starts with {economy.STARTING_BALANCE:,}. You can bet as much of "
                "it as you like."),
            color=0xFFB114)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Check your balance.")
    async def wallet(self, interaction: discord.Interaction):
        w = db.wallet(interaction.user.id, interaction.user.display_name)
        net = w["returned"] - w["staked"]
        await interaction.response.send_message(
            f"**{w['balance']:,} {CURRENCY}**\nStaked this season {w['staked']:,}, "
            f"returned {w['returned']:,} ({'+' if net >= 0 else ''}{net:,}).", ephemeral=True)

    @app_commands.command(description="See what you have riding on tonight's race.")
    async def mybets(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        if r is None:
            raise Refusal("No race yet.")
        rows = db.user_bets(r["id"], interaction.user.id)
        if not rows:
            raise Refusal("Nothing riding yet. `/payouts` explains how it pays.")
        lines = []
        for b in rows:
            label = b["label"] or db.market_label(b["kind"], b["key"])
            tail = ""
            if b["settled"]:
                tail = f" — returned {b['payout']:,}" if b["payout"] else " — no return"
            lines.append(f"{b['amount']:,} on {b['pick_text']} — {label}{tail}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(description="Show the board.")
    async def board(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        if r is None:
            raise Refusal("No race yet.")
        await interaction.response.send_message(embed=board_embed(r))

    @app_commands.command(description="Season standings.")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = db.leaderboard(10)
        if not rows:
            raise Refusal("Nobody has a wallet yet.")
        lines = [f"`{i:>2}.` **{x['display_name'] or x['user_id']}** — {x['balance']:,}"
                 for i, x in enumerate(rows, start=1)]
        season = db.current_season(interaction.guild_id)
        embed = discord.Embed(title=f"{season['name']} standings" if season else "Standings",
                              description="\n".join(lines), color=0xFFB114)
        foot = DISCLAIMER
        if season:
            foot = f"{db.season_races(season['id'])} of {economy.SEASON_LENGTH} broadcasts. " + foot
        embed.set_footer(text=foot)
        await interaction.response.send_message(embed=embed)


class Broadcast(commands.Cog):
    def __init__(self, bot: HorseRace):
        self.bot = bot

    race = app_commands.Group(name="race", description="Run the race.")
    season = app_commands.Group(name="season", description="The season standings.")

    @race.command(description="Set up this week's race.")
    @app_commands.describe(
        runners="The field in order, comma-separated, e.g. Mario, Luigi, Peach, Yoshi (2 to 12)",
        turns="How many turns the game is set to (default 35)",
        props="Include the minigames, coins and ? tiles side bets (off for e.g. Mario Kart)")
    @staff_only()
    async def create(self, interaction: discord.Interaction, week: str, runners: str,
                     game: str = "Mario Party",
                     turns: app_commands.Range[int, 1, 99] = db.DEFAULT_TURNS,
                     props: bool = True):
        names = [x.strip() for x in runners.split(",") if x.strip()]
        if not economy.MIN_RUNNERS <= len(names) <= economy.MAX_RUNNERS:
            raise Refusal(f"Between {economy.MIN_RUNNERS} and {economy.MAX_RUNNERS} runners, "
                          "separated by commas.")
        if len(set(x.lower() for x in names)) != len(names):
            raise Refusal("Every runner needs a different name.")
        if db.active_race(interaction.guild_id):
            raise Refusal("There's already a race in progress. Finish it first.")
        if db.current_season(interaction.guild_id) is None:
            db.start_season(interaction.guild_id, "Season 1")
        db.create_race(interaction.guild_id, week, game, names, turns, props)
        await interaction.response.send_message(
            f"**{week}** is built — {game}, {turns} turns, {len(names)} runners: "
            f"{', '.join(names)}." + ("" if props else " No props.") +
            "\n`/show start` to run it off the rundown, or `/race open` by hand.")

    @race.command(description="Open betting.")
    @staff_only()
    async def open(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        db.set_race_status(r["id"], "open")
        await interaction.response.send_message(
            content="**Betting is open.** `/bet` to guess the order, `/payouts` for how it pays.",
            embed=board_embed(db.race(r["id"])))

    @race.command(description="Close betting.")
    @staff_only()
    async def lock(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        db.set_race_status(r["id"], "locked")
        await interaction.response.send_message(
            content="**Betting is locked.** Here's where the money went.",
            embed=board_embed(db.race(r["id"])))

    @race.command(description="Enter the final finishing order. Pays every guess.")
    @app_commands.describe(**PLACE_HELP)
    @app_commands.autocomplete(**{p: entrant_options for p in PLACES})
    @staff_only()
    async def result(self, interaction: discord.Interaction,
                     first: str, second: str, third: str | None = None, fourth: str | None = None,
                     fifth: str | None = None, sixth: str | None = None,
                     seventh: str | None = None, eighth: str | None = None,
                     ninth: str | None = None, tenth: str | None = None,
                     eleventh: str | None = None, twelfth: str | None = None):
        r = require_race(interaction.guild_id)
        order = gather_order(first=first, second=second, third=third, fourth=fourth,
                             fifth=fifth, sixth=sixth, seventh=seventh, eighth=eighth,
                             ninth=ninth, tenth=tenth, eleventh=eleventh, twelfth=twelfth)
        out = db.settle_order(r["id"], order)
        if "error" in out:
            raise Refusal(out["error"])
        await interaction.response.send_message(call_text(out))

    @race.command(description="Call a prop by hand.")
    @app_commands.choices(market=PROP_CHOICES)
    @app_commands.autocomplete(winner=entrant_options)
    @staff_only()
    async def call(self, interaction: discord.Interaction,
                   market: app_commands.Choice[str], winner: str):
        r = require_race(interaction.guild_id)
        kind, key = split_market(market.value)
        await interaction.response.send_message(call_text(db.call_market(r["id"], kind, key, winner)))

    @race.command(description="Grade ? tiles and minigames from the live tally. Ties void.")
    @staff_only()
    async def autograde(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        results = db.autograde(r["id"])
        if not results:
            raise Refusal("Nothing to autograde: no props, or both are already called.")
        warn = ""
        if r["turn"] < r["total_turns"]:
            warn = f"\n_Heads up: the turn counter only reads {r['turn']} of {r['total_turns']}._"
        parts = []
        for x in results:
            counts = ", ".join(f"{n} {c}" for n, c in x["counts"].items())
            parts.append(call_text(x) + f"\nFinal count: {counts}")
        await interaction.response.send_message("\n\n".join(parts) + warn)

    @race.command(description="Void a market and refund it (tie, misplay, bad data).")
    @app_commands.choices(market=VOID_CHOICES)
    @staff_only()
    async def void(self, interaction: discord.Interaction, market: app_commands.Choice[str]):
        r = require_race(interaction.guild_id)
        kind, key = split_market(market.value)
        out = (db.settle_order(r["id"], None) if kind == "slate"
               else db.call_market(r["id"], kind, key, None))
        await interaction.response.send_message(call_text(out))

    @race.command(description="Close the night once everything is settled.")
    @staff_only()
    async def finish(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        out = db.finish_race(r["id"])
        if "error" in out:
            raise Refusal(out["error"])
        if out["finish"]:
            order = ", ".join(f"{db.ordinal(i)} {x}" for i, x in enumerate(out["finish"], 1))
        else:
            order = "voided, every guess refunded"
        best = [g for g in out["guesses"] if g["returned"] > g["staked"]][:6]
        body = "\n".join(f"**{g['name']}** {g['right']} right, {g['staked']:,} became "
                          f"{g['returned']:,}" for g in best) or "Nobody came out ahead on the order."
        n = len(out["finish"] or [])
        if n and any(g["right"] == n for g in out["guesses"]):
            body = "**PERFECT CARD.** Somebody called the whole order.\n\n" + body
        embed = discord.Embed(title=f"{r['week_label']} is in the books",
                              description=f"Finish: {order}\n\n{body}", color=0xFFB114)
        embed.add_field(name="Standings", inline=False, value="\n".join(
            f"`{i:>2}.` **{x['display_name']}** — {x['balance']:,}"
            for i, x in enumerate(db.leaderboard(5), start=1)) or "—")
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed)

    @season.command(description="Where the season is up to.")
    async def status(self, interaction: discord.Interaction):
        s = db.current_season(interaction.guild_id)
        if s is None:
            raise Refusal("No season yet. One opens with the first `/race create`.")
        done = db.season_races(s["id"])
        left = max(0, economy.SEASON_LENGTH - done)
        tail = ("Season is complete." if not left else
                "Final broadcast of the season." if left == 1 else f"{left} broadcasts left.")
        await interaction.response.send_message(
            f"**{s['name']}** — {done} of {economy.SEASON_LENGTH} run. {tail}")

    @season.command(description="Past season winners.")
    async def hall(self, interaction: discord.Interaction):
        rows = db.hall_of_fame(interaction.guild_id)
        if not rows:
            raise Refusal("No seasons finished yet.")
        lines, seen = [], None
        for r in rows:
            if r["season"] != seen:
                lines.append(f"\n**{r['season']}**")
                seen = r["season"]
            lines.append(f"`{r['rank']}.` {r['display_name']} — {r['balance']:,}")
        embed = discord.Embed(title="Past seasons", description="\n".join(lines).strip(),
                              color=0xFFB114)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Server host: wipe every wallet and start a new season.")
    @app_commands.describe(name="What to call the new season, e.g. Season 2 or Mario Kart")
    @app_commands.default_permissions(manage_guild=True)
    @host_only()
    async def reset(self, interaction: discord.Interaction, name: str):
        if db.active_race(interaction.guild_id):
            raise Refusal("Finish the race in progress before resetting.")
        players = len(db.leaderboard(10_000))
        await interaction.response.send_message(
            f"This puts all {players} wallets back to {economy.STARTING_BALANCE:,} and starts "
            f"**{name}**. The current table is saved to `/season hall` first. Sure?",
            view=ConfirmReset(interaction.user.id, interaction.guild_id, name), ephemeral=True)

    @app_commands.command(description="Server host: download a copy of the whole database.")
    @app_commands.default_permissions(manage_guild=True)
    @host_only()
    async def backup(self, interaction: discord.Interaction):
        stamp = time.strftime("%Y-%m-%d-%H%M")
        path = Path(tempfile.mkdtemp()) / f"horserace-{stamp}.db"
        db.snapshot(path)
        await interaction.response.send_message(
            "Here's a full copy of the season: every wallet, race, bet and past season. "
            "Keep it somewhere safe.", file=discord.File(path), ephemeral=True)

    @app_commands.command(description="Post the crew control panel: tallies, turns, undo.")
    @staff_only()
    async def panel(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        await interaction.response.send_message(panel_text(r["id"]), view=panel_view(r["id"]))

    @app_commands.command(description="Count a ? tile or minigame win by hand.")
    @app_commands.choices(what=TALLY_CHOICES)
    @app_commands.autocomplete(pick=entrant_options)
    @staff_only()
    async def tally(self, interaction: discord.Interaction, what: app_commands.Choice[str],
                    pick: str, delta: int = 1):
        r = require_race(interaction.guild_id)
        count = db.bump_tally(r["id"], pick, delta, what.value)
        await interaction.response.send_message(f"{pick} at {count} ({what.name}).", ephemeral=True)

    @app_commands.command(description="Top anyone who's broke back up to the rail floor.")
    @staff_only()
    async def railmoney(self, interaction: discord.Interaction, week: str):
        out = db.top_up_wallets(week)
        if not out["topped"]:
            await interaction.response.send_message(
                f"Everyone's above {economy.RAIL_FLOOR:,} already. Nothing paid out.")
            return
        await interaction.response.send_message(
            f"Topped {out['topped']} of {out['checked']} wallets back up to "
            f"{economy.RAIL_FLOOR:,} for {week}. {out['coins']:,} {CURRENCY} in.")

    @app_commands.command(description="Put someone's bets on the broadcast layout.")
    @app_commands.describe(user="Whose slip to show", on_air_name="Name to show on stream",
                           show="Off takes them back off the layout")
    @staff_only()
    async def feature(self, interaction: discord.Interaction, user: discord.Member,
                      on_air_name: str = "", show: bool = True):
        who = on_air_name or user.display_name
        db.set_featured(user.id, show, who)
        if show:
            shown = ", ".join(w["on_air_name"] or w["display_name"] for w in db.featured_users())
            msg = f"{who}'s slip is on the layout. Showing: {shown}."
        else:
            msg = f"{who} is off the layout."
        await interaction.response.send_message(msg, ephemeral=True)


async def template_options(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=name, value=name)
            for name in db.show_templates() if current.lower() in name.lower()][:25]


class Show(commands.Cog):
    """The rundown. Advancing it is the one command a producer needs."""

    def __init__(self, bot: HorseRace):
        self.bot = bot

    show = app_commands.Group(name="show", description="Run the broadcast rundown.")

    def _moved(self, out: dict) -> str:
        seg = out["segment"]
        line = f"**{seg['name']}** ({out['index'] + 1} of {out['count']}) — {seg['minutes']} min."
        if out["action"] == "open":
            line += "\nBetting is now **open**."
        elif out["action"] == "lock":
            line += "\nBetting is now **locked**."
        if seg.get("kind") == "game":
            line += "\nGame segment: the overlay switches to turn count and tallies. `/panel` if you haven't."
        return line

    @show.command(description="Start the rundown for tonight's race.")
    @app_commands.autocomplete(template=template_options)
    @staff_only()
    async def start(self, interaction: discord.Interaction, template: str = "standard"):
        r = require_race(interaction.guild_id)
        out = db.start_show(r["id"], template)
        if "error" in out:
            raise Refusal(out["error"])
        await interaction.response.send_message(self._moved(out))

    @show.command(description="Move to the next segment.")
    @staff_only()
    async def next(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        out = db.move_segment(r["id"], 1)
        if "error" in out:
            raise Refusal(out["error"])
        await interaction.response.send_message(self._moved(out))

    @show.command(description="Go back a segment (doesn't undo betting changes).")
    @staff_only()
    async def back(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        out = db.move_segment(r["id"], -1)
        if "error" in out:
            raise Refusal(out["error"])
        await interaction.response.send_message(self._moved(out), ephemeral=True)

    @show.command(description="Show tonight's rundown.")
    async def rundown(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        if r is None:
            raise Refusal("No race yet.")
        await interaction.response.send_message(rundown_text(r), ephemeral=True)


async def bonus_options(interaction: discord.Interaction, current: str):
    r = db.active_race(interaction.guild_id)
    if r is None:
        return []
    return [app_commands.Choice(name=m["label"][:100], value=str(m["id"]))
            for m in db.bonus_markets(r["id"], include_settled=False)
            if current.lower() in m["label"].lower()][:25]


async def bonus_answer_options(interaction: discord.Interaction, current: str):
    try:
        m = db.market_by_id(int(interaction.namespace.market))
    except (TypeError, ValueError, AttributeError):
        return []
    if m is None:
        return []
    return [app_commands.Choice(name=o, value=o)
            for o in db.options_of(m) if current.lower() in o.lower()][:25]


class Bonus(commands.Cog):
    """Quick mid-game markets, so the ninety minutes of racing aren't dead air
    for anyone who already placed their pre-show bets."""

    def __init__(self, bot: HorseRace):
        self.bot = bot

    bonus = app_commands.Group(name="bonus", description="Mid-game bonus questions.")

    async def _close_later(self, market_id: int, message: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        db.lock_expired_bonuses()
        m = db.market_by_id(market_id)
        try:
            await message.edit(embed=bonus_embed(m), view=bonus_view(market_id, disabled=True))
        except discord.HTTPException:
            pass  # message deleted or channel gone; the clock still closed the market

    @bonus.command(description="Open a quick bonus question during the race.")
    @app_commands.describe(
        question="e.g. Who wins the next minigame?",
        options="Comma-separated answers. Leave blank for tonight's four characters.",
        seconds="How long betting stays open (default 90)",
        pays="Multiplier. Leave blank for one better than fair.")
    @staff_only()
    async def open(self, interaction: discord.Interaction, question: str, options: str = "",
                   seconds: app_commands.Range[int, 15, 600] = 90,
                   pays: app_commands.Range[int, 2, 20] | None = None):
        r = require_race(interaction.guild_id)
        answers = [o.strip() for o in options.split(",") if o.strip()] if options else \
            [e["name"] for e in db.entrants(r["id"])]
        if not 2 <= len(answers) <= 10 or len(set(a.lower() for a in answers)) != len(answers):
            raise Refusal("Between 2 and 10 different answers, please.")
        market_id = db.open_bonus(r["id"], question, answers, seconds, pays)
        m = db.market_by_id(market_id)
        await interaction.response.send_message(embed=bonus_embed(m), view=bonus_view(market_id))
        message = await interaction.original_response()
        db.attach_message(market_id, message.channel.id, message.id)
        self.bot.spawn(self._close_later(market_id, message, seconds))

    @bonus.command(description="Settle a bonus question. Pays out immediately.")
    @app_commands.autocomplete(market=bonus_options, winner=bonus_answer_options)
    @staff_only()
    async def call(self, interaction: discord.Interaction, market: str, winner: str):
        require_race(interaction.guild_id)
        try:
            market_id = int(market)
        except ValueError:
            raise Refusal("Pick the bonus from the list.")
        db.lock_expired_bonuses()
        m = db.market_by_id(market_id)
        if m is not None and m["status"] == "open" and m["result"] is None:
            raise Refusal("That bonus is still taking bets. Let the clock run out first.")
        await interaction.response.send_message(call_text(db.call_market_id(market_id, winner)))

    @bonus.command(description="Void a bonus question and refund it.")
    @app_commands.autocomplete(market=bonus_options)
    @staff_only()
    async def void(self, interaction: discord.Interaction, market: str):
        require_race(interaction.guild_id)
        try:
            market_id = int(market)
        except ValueError:
            raise Refusal("Pick the bonus from the list.")
        await interaction.response.send_message(call_text(db.call_market_id(market_id, None)))


async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, (Refusal, app_commands.CheckFailure)):
        message = str(original)
    else:
        message = "Something broke on our end. Ping a mod."
        # Loud in the console so a broadcast-night failure can be diagnosed.
        command = getattr(interaction.command, "qualified_name", "?")
        log.error("/%s failed", command, exc_info=original)
    send = (interaction.followup.send if interaction.response.is_done()
            else interaction.response.send_message)
    await send(message, ephemeral=True)


def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN isn't set. Copy it from the Bot page of the "
                         "Discord developer portal into your Railway variables.")
    bot = HorseRace()
    bot.tree.on_error = on_error
    try:
        bot.run(token, root_logger=True)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected DISCORD_TOKEN. Reset the token on the Bot page "
                         "of the developer portal and paste the new one in.")


if __name__ == "__main__":
    main()
