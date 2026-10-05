"""Mario Party Horse Racing — Discord bot.

Players:  /bet  /payouts  /wallet  /mybets  /board  /leaderboard  /season ...
Crew:     /race ...  /show ...  /bonus ...  /panel  /tally  /railmoney  /feature

Crew commands need the role named in MPR_STAFF_ROLE (default "Race Staff"),
or Manage Server.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from . import ads as adrules
from . import db, economy, overlay

STAFF_ROLE = os.getenv("MPR_STAFF_ROLE", "Race Staff")
CURRENCY = os.getenv("MPR_CURRENCY", "points")
GUILD_ID = int(os.getenv("MPR_GUILD_ID", "0"))
DISCLAIMER = "No real money. All bets use fake Discord points."
DEFAULT_BONUS_STAKE = 100
log = logging.getLogger("mpr")

PROP_CHOICES = [
    app_commands.Choice(name="First to get a star", value="prop:firststar"),
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


# Discord hides crew commands from anyone without this permission. Give it to
# the Race Staff role (Server Settings, Roles) and the crew sees everything.
# The Race Staff check below still runs, so visibility is never the only lock.
CREW_VISIBLE = discord.Permissions(manage_events=True)


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


def host_only(action: str = "do that"):
    """For the server host: anyone with Manage Server."""
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is not None and perms.manage_guild:
            return True
        raise app_commands.CheckFailure(f"Only the server host can {action}.")
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
    records = db.form(names, race["mode"], race["guild_id"])
    embed.add_field(name="Form, latest first", inline=False, value="\n".join(
        f"**{x}**: {form_line(records[x])}" for x in names[:12])[:1024])
    for m in db.markets(race["id"], ("prop",)):
        totals = db.market_totals(m["id"])
        staked = sum(totals.values())
        lines = [f"`{x:<10} {totals.get(x, 0):>5,}`" for x in names if totals.get(x, 0)]
        embed.add_field(name=f"{db.label_of(m)} — {staked:,} down",
                        value="\n".join(lines) or "No bets yet.", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def form_line(rec: dict) -> str:
    if not rec["starts"]:
        return "debut, no form yet"
    recent = ", ".join(db.ordinal(x) for x in rec["recent"])
    wins = f"{rec['wins']} win" + ("" if rec["wins"] == 1 else "s")
    return f"{recent} ({wins} from {rec['starts']}, average {rec['average']})"


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
    unit = db.MODES[r["mode"]]["unit"]
    last5 = r["mode"] == "party" and r["total_turns"] - r["turn"] < 5 and r["turn"] > 0
    turn = f"{unit.capitalize()} {r['turn']} of {r['total_turns']}" + \
        (", last five turns" if last5 else "")
    star = db.market(race_id, "prop", "firststar")
    star_line = ""
    if star is not None:
        star_line = ("\nFirst star: " + (star["result"] if star["result"] not in (None, "VOID")
                                          else "nobody yet, tap ★ when it happens"))
    return (f"**{r['week_label']} control panel** — {turn}{star_line}\n"
            "```\n" + f"{'':<10} {'? tiles':>4} {'games':>6}\n" + "\n".join(rows) + "\n```"
            "Tap a character each time it happens. Undo reverses the last tap. "
            "\\* marks the outright leader. 🎮 opens a 60-second bet on the next minigame.")


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


def night_embed(week: str, finish: list[str] | None, guesses: list[dict],
                props: list[dict] | None = None) -> discord.Embed:
    """The post-game card: final order, props, who came out ahead, standings."""
    if finish:
        order = ", ".join(f"{db.ordinal(i)} {x}" for i, x in enumerate(finish, 1))
    else:
        order = "voided, every guess refunded"
    lines = [f"**Final order:** {order}"]
    for x in props or []:
        if "error" in x:
            continue
        counts = x.get("counts")
        count_text = f" ({', '.join(f'{n} {c}' for n, c in counts.items())})" if counts else ""
        if x["voided"]:
            lines.append(f"**{x['label']}:** tie, bets refunded{count_text}")
        else:
            lines.append(f"**{x['label']}:** {x['winner']}, {x['winners']} of "
                         f"{x['tickets']} cashed{count_text}")
    ahead = [g for g in guesses if g["returned"] > g["staked"]][:6]
    lines.append("")
    lines += [f"**{g['name']}** {g['right']} right, {g['staked']:,} became {g['returned']:,}"
              for g in ahead] or ["Nobody came out ahead on the order."]
    n = len(finish or [])
    if n and any(g["right"] == n for g in guesses):
        lines.insert(0, "**PERFECT CARD.** Somebody called the whole order.\n")
    embed = discord.Embed(title=f"{week} is in the books", description="\n".join(lines)[:4000],
                          color=0xFFB114)
    embed.add_field(name="Standings", inline=False, value="\n".join(
        f"`{i:>2}.` **{x['display_name']}** — {x['balance']:,}"
        for i, x in enumerate(db.leaderboard(5), start=1)) or "—")
    embed.set_footer(text=DISCLAIMER)
    return embed


SLOT_ICONS = ["🟥", "🟩", "🟧", "🟦", "🟪", "🟨"]   # match the overlay's runner colours


def runner_icon(guild, name: str, slot: int | None) -> str:
    """A server emoji named like the runner (":Mario:") if there is one,
    otherwise a coloured square matching their colour on stream."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for emoji in getattr(guild, "emojis", None) or []:
        if re.sub(r"[^a-z0-9]", "", emoji.name.lower()) == key:
            return str(emoji)
    if slot and 1 <= slot <= len(SLOT_ICONS):
        return SLOT_ICONS[slot - 1]
    return "▫️"


def with_icons(guild, race_id: int, names: list[str]) -> str:
    slots = {e["name"]: e["slot"] for e in db.entrants(race_id)}
    return " › ".join(f"{runner_icon(guild, n, slots.get(n))} {n}" for n in names)


def bonus_status(m) -> str:
    if m["result"] is not None:
        if m["result"] == "VOID":
            return "voided, every stake refunded"
        return f"**{m['result'].replace(' + ', ' and ')}** " + \
            ("win" if " + " in m["result"] else "wins")
    if db.bonus_taking_bets(m):
        if m["closes_at"] is None:
            return "open until betting locks for the race"
        return f"closes <t:{int(m['closes_at'])}:R>"
    return "betting closed, waiting for the result"


def bonus_embed(m) -> discord.Embed:
    options = db.options_of(m)
    backers = db.bonus_backers(m["id"])
    staked = sum(a for rows in backers.values() for _, a, _ in rows)
    lines = []
    for o in options:
        rows = backers.get(o, [])
        total = sum(a for _, a, _ in rows)
        price = db.bonus_price(m, o)
        names = [f"{n} {a:,}" if not free else f"{n} (free pick)" for n, a, free in rows[:6]]
        if len(rows) > 6:
            names.append(f"+{len(rows) - 6} more")
        who = ", ".join(names) if names else "nobody yet"
        lines.append(f"**{o}** ({price}x) — {total:,} in: {who}")
    rules = []
    if m["max_picks"] == 1:
        rules.append("Back one answer.")
    elif m["max_picks"]:
        rules.append(f"Back up to {m['max_picks']} answers.")
    if m["bonus_type"] == "minigame":
        rules.append(f"Out of points? Tap an answer for a free pick worth {economy.COMEBACK_PRIZE}.")
    embed = discord.Embed(
        title=f"Bonus: {m['label']}",
        description=(f"Pays **{m['multiplier']}x**, {bonus_status(m)}.\n"
                     + (" ".join(rules) + "\n" if rules else "")
                     + f"Tap an answer to bet. Default stake {DEFAULT_BONUS_STAKE}.\n\n"
                     + "\n".join(lines)[:3500] + f"\n\n{staked:,} {CURRENCY} down"),
        color=0xFFA81E)
    embed.set_footer(text=DISCLAIMER)
    return embed


# ------------------------------------------------------ plain-logic handlers
# Buttons and modals call these. Kept free of discord objects so they can be
# tested directly.


def status_embed(guild_id: int, user_id: int, display_name: str) -> discord.Embed:
    """Everything a player wants to know about themselves, in one place."""
    w = db.wallet(user_id, display_name)
    place, players = db.rank_of(user_id)
    season = db.current_season(guild_id)
    title = f"{display_name}, {season['name']}" if season else display_name
    net = w["returned"] - w["staked"]
    lines = [
        f"**{w['balance']:,} {CURRENCY}**, {db.ordinal(place)} of {players}",
        f"This season: bet {w['staked']:,}, won back {w['returned']:,} "
        f"({'+' if net >= 0 else ''}{net:,})",
    ]
    if w["adjusted"]:
        lines.append(f"Gifts from the crew this season: {'+' if w['adjusted'] > 0 else ''}"
                     f"{w['adjusted']:,}")
    if w["balance"] < economy.MIN_WAGER:
        lines.append("You're out of points. The crew can top you up before the next show.")

    r = db.active_race(guild_id) or db.latest_race(guild_id)
    if r is not None:
        bets = db.user_bets(r["id"], user_id)
        state = {"open": "betting open", "locked": "betting locked", "draft": "not open yet",
                 "settled": "finished"}.get(r["status"], r["status"])
        lines += ["", f"**{r['week_label']}** ({state})"]
        if not bets:
            lines.append("No bets. `/bet` to guess the order, `/sidebet` for side bets."
                         if r["status"] == "open" else "You didn't bet on this one.")
        for b in bets:
            label = b["label"] or db.market_label(b["kind"], b["key"])
            if not b["settled"]:
                outcome = "riding"
            elif b["payout"]:
                outcome = f"won {b['payout']:,}" if b["payout"] > b["amount"] else \
                          f"got {b['payout']:,} back"
            else:
                outcome = "lost"
            lines.append(f"{b['amount']:,} on {b['pick_text']}, {label}: {outcome}")

    waiting = db.ads(status="pending", submitted_by=user_id)
    live = db.ads(status="live", submitted_by=user_id)
    if waiting or live:
        bits = []
        if live:
            bits.append(f"{len(live)} on stream")
        if waiting:
            bits.append(f"{len(waiting)} waiting for review")
        lines += ["", "Your ads: " + ", ".join(bits)]

    embed = discord.Embed(title=title, description="\n".join(lines)[:4000], color=0xFFB114)
    embed.set_footer(text=DISCLAIMER)
    return embed


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
    """A viewer backing an answer in a bonus market. Returns the reply."""
    return bonus_stake(user_id, display_name, market_id, option_index, raw_amount)[0]


def bonus_stake(user_id: int, display_name: str, market_id: int,
                option_index: int, raw_amount: str) -> tuple[str, str, int | None]:
    """(reply for the viewer, public callout or '', the new bet's id or None)."""
    m = db.market_by_id(market_id)
    if m is None:
        return "That bonus is gone.", "", None
    options = db.options_of(m)
    if not 0 <= option_index < len(options):
        return "That answer isn't on this bonus.", "", None
    try:
        amount = int(str(raw_amount).replace(",", "").strip())
    except ValueError:
        return "Whole points only.", "", None
    db.wallet(user_id, display_name)
    pick = options[option_index]
    ok, why = db.place_bet(m["race_id"], market_id, user_id, pick, amount)
    if not ok:
        return why, "", None
    left = db.wallet(user_id)["balance"]
    return (f"{amount:,} on **{pick}**. Pays {db.bonus_price(m, pick)}x if right. "
            f"{left:,} {CURRENCY} left."), why, db.last_bet_id(user_id, market_id)


# ----------------------------------------------------------------- buttons


class TallyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"mpr:t:(?P<race>\d+):(?P<what>qt|mg|tn|ud|st|mb):(?P<arg>-?\d+)"):
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
        if self.what == "mb":
            r = db.active_race(interaction.guild_id)
            if r is None or r["id"] != self.race_id:
                await interaction.response.send_message(
                    "This panel is for an old race. Run /panel again.", ephemeral=True)
                return
            names = [e["name"] for e in db.entrants(self.race_id)]
            await open_bonus_post(interaction, self.race_id, "Who wins this minigame?",
                                  "minigame", names + [economy.DRAW], 60)
            return
        if self.what == "st":
            e = db.entrant_by_slot(self.race_id, self.arg)
            out = db.call_market(self.race_id, "prop", "firststar", e["name"])
            if "error" in out:
                await interaction.response.send_message(out["error"], ephemeral=True)
                return
            await interaction.response.edit_message(content=panel_text(self.race_id),
                                                    view=panel_view(self.race_id))
            await interaction.followup.send(
                f"⭐ **First star: {e['name']}!** {out['winners']} of {out['tickets']} "
                f"side bets cashed, {out['paid']:,} {CURRENCY} paid out.")
            return
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
    unit = db.MODES[db.race(race_id)["mode"]]["unit"]
    view.add_item(TallyButton(race_id, "tn", 1, f"Next {unit}", discord.ButtonStyle.secondary, row=2))
    view.add_item(TallyButton(race_id, "tn", -1, f"{unit.capitalize()} back",
                              discord.ButtonStyle.secondary, row=2))
    view.add_item(TallyButton(race_id, "ud", 0, "Undo last", discord.ButtonStyle.danger, row=2))
    star = db.market(race_id, "prop", "firststar")
    if star is not None and len(ents) <= 4:
        if star["result"] is None:
            for e in ents:
                view.add_item(TallyButton(race_id, "st", e["slot"], f"★ {e['name']}",
                                          discord.ButtonStyle.secondary, row=3))
        view.add_item(TallyButton(race_id, "mb", 0, "🎮 Minigame bet",
                                  discord.ButtonStyle.success, row=3))
    return view


class UndoButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"mpr:undo:(?P<bet>\d+)"):
    """On every bet confirmation: take the bet back while betting's open."""

    def __init__(self, bet_id: int):
        super().__init__(discord.ui.Button(label="Undo this bet", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mpr:undo:{bet_id}"))
        self.bet_id = bet_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["bet"]))

    async def callback(self, interaction: discord.Interaction):
        ok, why, info = db.cancel_bet(interaction.user.id, self.bet_id)
        if not ok:
            await interaction.response.send_message(why, ephemeral=True)
            return
        balance = db.wallet(interaction.user.id)["balance"]
        await interaction.response.edit_message(
            content=f"Taken back: {info['amount']:,} on {info['pick_text']} ({info['label']}). "
                    f"You have {balance:,} {CURRENCY}.", view=None)
        if info["kind"] == "bonus":
            await refresh_bonus_post(interaction.client, info["market_id"])


def undo_view(bet_id: int | None) -> discord.ui.View | None:
    if bet_id is None:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(UndoButton(bet_id))
    return view


class BonusStakeModal(discord.ui.Modal):
    def __init__(self, market_id: int, option_index: int, option: str):
        super().__init__(title=f"Back {option}"[:45])
        self.market_id, self.option_index = market_id, option_index
        self.amount = discord.ui.TextInput(
            label=f"How many {CURRENCY}?", default=str(DEFAULT_BONUS_STAKE), max_length=7)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        text, callout, bet_id = bonus_stake(interaction.user.id, interaction.user.display_name,
                                            self.market_id, self.option_index, self.amount.value)
        await interaction.response.send_message(text, ephemeral=True,
                                                **({"view": undo_view(bet_id)} if bet_id else {}))
        await refresh_bonus_post(getattr(interaction, "client", None), self.market_id,
                                 getattr(interaction, "message", None))
        if callout:
            await interaction.followup.send(callout)


class BonusButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"mpr:b:(?P<market>\d+):(?P<idx>\d+)"):
    def __init__(self, market_id: int, idx: int, label: str = "·"):
        super().__init__(discord.ui.Button(
            label=label[:80], style=discord.ButtonStyle.primary,
            custom_id=f"mpr:b:{market_id}:{idx}", row=min(3, idx // 5)))
        self.market_id, self.idx = market_id, idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["market"]), int(match["idx"]), item.label)

    async def callback(self, interaction: discord.Interaction):
        m = db.market_by_id(self.market_id)
        if m is None or not db.bonus_taking_bets(m):
            await interaction.response.send_message("That bonus is closed.", ephemeral=True)
            return
        option = db.options_of(m)[self.idx]
        balance = db.wallet(interaction.user.id, interaction.user.display_name)["balance"]
        if balance < economy.MIN_WAGER:
            # The comeback: a broke player still gets a free pick on minigame bets.
            ok, why = db.place_comeback(self.market_id, interaction.user.id,
                                        interaction.user.display_name, option)
            msg = (f"You're out of points, so this one's free: **{option}**. If it lands you get "
                   f"{economy.COMEBACK_PRIZE} {CURRENCY} to get back in." if ok else
                   why if m["bonus_type"] == "minigame" else
                   "You're out of points. Minigame bets give you a free pick, so wait for the next one.")
            await interaction.response.send_message(msg, ephemeral=True)
            if ok:
                await refresh_bonus_post(getattr(interaction, "client", None), self.market_id,
                                         getattr(interaction, "message", None))
            return
        await interaction.response.send_modal(BonusStakeModal(self.market_id, self.idx, option))


class BonusCrewButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"mpr:bc:(?P<market>\d+):(?P<action>close|pay|del)"):
    """The crew's row on every bonus post: close early, pay out, delete."""

    LABELS = {"close": ("Close now", discord.ButtonStyle.secondary),
              "pay": ("Pay out", discord.ButtonStyle.success),
              "del": ("Delete", discord.ButtonStyle.danger)}

    def __init__(self, market_id: int, action: str, disabled: bool = False):
        label, style = self.LABELS[action]
        button = discord.ui.Button(label=f"Crew: {label}", style=style, row=4,
                                   custom_id=f"mpr:bc:{market_id}:{action}", disabled=disabled)
        super().__init__(button)
        self.market_id, self.action = market_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["market"]), match["action"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Crew only.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        m = db.market_by_id(self.market_id)
        if m is None or m["result"] is not None:
            await interaction.response.send_message("That bonus is already settled.", ephemeral=True)
            return
        if self.action == "close":
            db.close_bonus(self.market_id)
            m = db.market_by_id(self.market_id)
            await interaction.response.edit_message(embed=bonus_embed(m),
                                                    view=bonus_view(self.market_id))
        elif self.action == "pay":
            db.close_bonus(self.market_id)               # paying out closes betting first
            await interaction.response.send_message(
                f"Who won **{m['label']}**?", view=PayoutPicker(self.market_id), ephemeral=True)
            await refresh_bonus_post(getattr(interaction, "client", None), self.market_id,
                                     getattr(interaction, "message", None))
        else:
            out = db.call_market_id(self.market_id, None)
            await interaction.response.send_message(
                f"Deleted **{m['label']}**. {out['tickets']} bets refunded.", ephemeral=True)
            try:
                await interaction.message.delete()
            except (discord.HTTPException, AttributeError):
                pass


VOID_PICK = "__void__"


class PayoutPicker(discord.ui.View):
    """Pick the winner (or two, for a 2 v 2 minigame) and pay out."""

    def __init__(self, market_id: int):
        super().__init__(timeout=300)
        self.market_id = market_id
        m = db.market_by_id(market_id)
        options = db.options_of(m)
        most = 2 if (m["max_picks"] or 1) >= 2 and len(options) > 2 else 1
        choices = [discord.SelectOption(label=o[:100], value=o) for o in options[:24]]
        choices.append(discord.SelectOption(label="Nobody: refund everyone", value=VOID_PICK))
        self.pick = discord.ui.Select(placeholder="Winner" + (" (or two winners for 2 v 2)" if most == 2 else ""),
                                      min_values=1, max_values=most, options=choices)
        self.pick.callback = self.chosen
        self.add_item(self.pick)

    async def chosen(self, interaction: discord.Interaction):
        values = list(self.pick.values)
        winners = None if VOID_PICK in values else values
        out = db.call_market_id(self.market_id, winners)
        if "error" in out:
            await interaction.response.edit_message(content=out["error"], view=None)
            return
        await interaction.response.edit_message(content="Paid out.", view=None)
        await refresh_bonus_post(getattr(interaction, "client", None), self.market_id)
        await interaction.followup.send(call_text(out))


def bonus_view(market_id: int, disabled: bool | None = None) -> discord.ui.View:
    m = db.market_by_id(market_id)
    closed = not db.bonus_taking_bets(m) if disabled is None else disabled
    settled = m["result"] is not None
    view = discord.ui.View(timeout=None)
    for i, option in enumerate(db.options_of(m)[:20]):
        item = BonusButton(market_id, i, option)
        item.item.disabled = closed
        view.add_item(item)
    if not settled:
        view.add_item(BonusCrewButton(market_id, "close", disabled=closed))
        view.add_item(BonusCrewButton(market_id, "pay"))
        view.add_item(BonusCrewButton(market_id, "del"))
    return view


async def refresh_bonus_post(client, market_id: int, message=None):
    """Bring the bonus post up to date: who's in, whether it's open, the result."""
    m = db.market_by_id(market_id)
    if m is None:
        return
    try:
        if message is None and client is not None and m["channel_id"] and m["message_id"]:
            channel = client.get_channel(m["channel_id"]) or await client.fetch_channel(m["channel_id"])
            message = channel.get_partial_message(m["message_id"])
        if message is not None:
            await message.edit(embed=bonus_embed(m), view=bonus_view(market_id))
    except (discord.HTTPException, AttributeError):
        pass       # post deleted, or no access; the bet itself already went through


async def close_later(client, market_id: int, seconds: int):
    await asyncio.sleep(seconds)
    db.lock_expired_bonuses()
    await refresh_bonus_post(client, market_id)


async def open_bonus_post(interaction: discord.Interaction, race_id: int, question: str,
                          bonus_type: str, answers: list[str], seconds: int | None,
                          pays: int | None = None):
    rules = economy.BONUS_TYPES.get(bonus_type, {"max_picks": 1})
    market_id = db.open_bonus(race_id, question, answers, seconds, pays, bonus_type,
                              rules["max_picks"])
    await interaction.response.send_message(embed=bonus_embed(db.market_by_id(market_id)),
                                            view=bonus_view(market_id))
    message = await interaction.original_response()
    db.attach_message(market_id, message.channel.id, message.id)
    client = getattr(interaction, "client", None)
    if seconds is not None and client is not None and hasattr(client, "spawn"):
        client.spawn(close_later(client, market_id, seconds))
    return market_id


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
        self.mode: str | None = None

    def install_mode(self, mode: str) -> bool:
        """Put this mode's /bet and /race result in the command tree.
        Returns True if anything changed."""
        bet, result = MODE_COMMANDS[mode]
        changed = False
        if self.tree.get_command("bet") is not bet:
            self.tree.add_command(bet, override=True)
            changed = True
        race = self.tree.get_command("race")
        if race is not None and race.get_command("result") is not result:
            race.add_command(result, override=True)
            changed = True
        self.mode = mode
        return changed

    async def apply_mode(self, mode: str):
        """Switch modes and tell Discord, which takes about a second."""
        if self.install_mode(mode):
            await self.sync_commands()
            log.info("Switched to %s: /bet now shows %s places.", db.MODES[mode]["label"],
                     db.MODES[mode]["runners"][1])

    async def sync_commands(self):
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

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def setup_hook(self):
        db.init()
        db.lock_expired_bonuses()
        self.add_dynamic_items(TallyButton, BonusButton, BonusCrewButton, UndoButton)
        for cog in COGS:
            await self.add_cog(cog(self))
        self.spawn(overlay.serve())
        # Come back up in the mode of whatever race is running, so a restart
        # mid-Kart-race doesn't put the four-place /bet back.
        running = db.active_race(GUILD_ID) if GUILD_ID else db.latest_race(None)
        self.install_mode(running["mode"] if running else "party")
        await self.sync_commands()

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


# ------------------------------------------------ mode-dependent commands
# Discord fixes a command's options when it's registered, so one /bet can't
# show four places one night and twelve the next. Instead there are two of
# each, and the bot swaps which one the server sees when the mode changes.

MODE_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in db.MODES.items()]
PARTY_PLACES = PLACES[:4]


async def place_guess(interaction: discord.Interaction, amount: int, order: list[str]):
    r = require_race(interaction.guild_id)
    db.wallet(interaction.user.id, interaction.user.display_name)
    ok, reason = db.place_slate(r["id"], interaction.user.id, order, amount)
    if not ok:
        raise Refusal(reason)
    callout = reason
    n = len(order)
    balance = db.wallet(interaction.user.id)["balance"]
    guild = getattr(interaction, "guild", None)
    bet_id = db.last_bet_id(interaction.user.id, db.slate_market(r["id"])["id"])
    await interaction.response.send_message(
        f"{amount:,} on {with_icons(guild, r['id'], order)}.\n"
        f"Get all {n} right and it comes back as {amount * economy.slate_multiplier(n):,}. "
        f"{ladder_line(n).capitalize()}.\nYou have {balance:,} {CURRENCY} left. "
        "Changed your mind? Undo it until betting locks.",
        ephemeral=True, view=undo_view(bet_id))
    # Everyone sees who's backing what; a big bet gets the louder callout instead.
    await interaction.followup.send(
        callout or f"**{interaction.user.display_name}** bet {amount:,}: "
                   f"{with_icons(guild, r['id'], order)}")


async def settle_result(interaction: discord.Interaction, order: list[str],
                        coins: str | None = None, first_star: str | None = None):
    """The whole post-game: tallied props, coins, every guess, close the night."""
    r = require_race(interaction.guild_id)
    out = db.settle_night(r["id"], order, coins, first_star)
    if "error" in out:
        raise Refusal(out["error"])
    embed = night_embed(out["week"], out["finish"], out["guesses"], out["props"])
    if out.get("refunded_bonuses"):
        embed.add_field(name="Refunded, never settled", inline=False,
                        value=", ".join(out["refunded_bonuses"])[:1000])
    await interaction.response.send_message(embed=embed)


async def coins_options(interaction: discord.Interaction, current: str):
    names = await entrant_options(interaction, current)
    if "tie".startswith(current.lower()) or current.lower() in "tie, refund the bets":
        names = names[:24] + [app_commands.Choice(name="Tie, refund the bets", value=db.TIE)]
    return names


BET_HELP = "Guess the finishing order and put points on it."
RESULT_HELP = "After the race: enter the order (and coins). Settles everything and closes the night."


@app_commands.command(name="bet", description=BET_HELP)
@app_commands.describe(amount="How many points to stake on this guess",
                       **{p: PLACE_HELP[p] for p in PARTY_PLACES})
@app_commands.autocomplete(**{p: entrant_options for p in PARTY_PLACES})
async def bet_party(interaction: discord.Interaction, amount: int,
                    first: str, second: str, third: str, fourth: str):
    await place_guess(interaction, amount, [first, second, third, fourth])


@app_commands.command(name="bet", description=BET_HELP)
@app_commands.describe(amount="How many points to stake on this guess", **PLACE_HELP)
@app_commands.autocomplete(**{p: entrant_options for p in PLACES})
async def bet_kart(interaction: discord.Interaction, amount: int,
                   first: str, second: str, third: str | None = None, fourth: str | None = None,
                   fifth: str | None = None, sixth: str | None = None,
                   seventh: str | None = None, eighth: str | None = None,
                   ninth: str | None = None, tenth: str | None = None,
                   eleventh: str | None = None, twelfth: str | None = None):
    await place_guess(interaction, amount, gather_order(
        first=first, second=second, third=third, fourth=fourth, fifth=fifth, sixth=sixth,
        seventh=seventh, eighth=eighth, ninth=ninth, tenth=tenth, eleventh=eleventh,
        twelfth=twelfth))


@app_commands.command(name="result", description=RESULT_HELP)
@app_commands.describe(coins="Who had the most coins when the race ended. Pick Tie for a tie",
                       first_star="Only if you didn't tap ★ on the panel: who got the first star",
                       **{p: PLACE_HELP[p] for p in PARTY_PLACES})
@app_commands.autocomplete(coins=coins_options, first_star=entrant_options,
                           **{p: entrant_options for p in PARTY_PLACES})
@staff_only()
async def result_party(interaction: discord.Interaction,
                       first: str, second: str, third: str, fourth: str, coins: str,
                       first_star: str | None = None):
    await settle_result(interaction, [first, second, third, fourth], coins, first_star)


@app_commands.command(name="result", description=RESULT_HELP)
@app_commands.describe(**PLACE_HELP)
@app_commands.autocomplete(**{p: entrant_options for p in PLACES})
@staff_only()
async def result_kart(interaction: discord.Interaction,
                      first: str, second: str, third: str | None = None, fourth: str | None = None,
                      fifth: str | None = None, sixth: str | None = None,
                      seventh: str | None = None, eighth: str | None = None,
                      ninth: str | None = None, tenth: str | None = None,
                      eleventh: str | None = None, twelfth: str | None = None):
    await settle_result(interaction, gather_order(
        first=first, second=second, third=third, fourth=fourth, fifth=fifth, sixth=sixth,
        seventh=seventh, eighth=eighth, ninth=ninth, tenth=tenth, eleventh=eleventh,
        twelfth=twelfth))


MODE_COMMANDS = {"party": (bet_party, result_party), "kart": (bet_kart, result_kart)}


async def cancel_options(interaction: discord.Interaction, current: str):
    r = db.active_race(interaction.guild_id)
    if r is None:
        return []
    bets = db.cancellable_bets(r["id"], interaction.user.id)
    choices = [app_commands.Choice(name=f"{b['amount']:,} on {b['pick_text']} ({b['label']})"[:100],
                                   value=str(b["id"])) for b in bets]
    if len(bets) > 1:
        choices.insert(0, app_commands.Choice(name=f"All {len(bets)} of my open bets", value="all"))
    return [c for c in choices if current.lower() in c.name.lower()][:25]


class Betting(commands.Cog):
    def __init__(self, bot: HorseRace):
        self.bot = bot

    @app_commands.command(description="Take back a bet while betting's still open. You get the points back.")
    @app_commands.describe(bet="Which bet to take back. Pick from the list")
    @app_commands.autocomplete(bet=cancel_options)
    async def cancel(self, interaction: discord.Interaction, bet: str):
        r = require_race(interaction.guild_id)
        if bet == "all":
            ids = [b["id"] for b in db.cancellable_bets(r["id"], interaction.user.id)]
            if not ids:
                raise Refusal("You don't have any bets you can take back right now.")
        else:
            try:
                ids = [int(bet)]
            except ValueError:
                raise Refusal("Pick the bet from the list.")
        lines, refunded = [], 0
        for bet_id in ids:
            ok, why, info = db.cancel_bet(interaction.user.id, bet_id)
            if not ok:
                raise Refusal(why)
            refunded += info["amount"]
            lines.append(f"Took back {info['amount']:,} on {info['pick_text']} ({info['label']}).")
        balance = db.wallet(interaction.user.id)["balance"]
        lines.append(f"{refunded:,} {CURRENCY} back, you have {balance:,}. "
                     "`/bet` or `/sidebet` to place a new one.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="sidebet",
                          description="Side bet: first star, most minigames, most coins, or most ? tiles.")
    @app_commands.describe(market="Which side bet", pick="Which character",
                           amount=f"How many {CURRENCY} to stake")
    @app_commands.choices(market=PROP_CHOICES)
    @app_commands.autocomplete(pick=entrant_options)
    async def sidebet(self, interaction: discord.Interaction,
                      market: app_commands.Choice[str], pick: str, amount: int):
        r = require_race(interaction.guild_id)
        db.wallet(interaction.user.id, interaction.user.display_name)
        _, key = split_market(market.value)
        m = db.market(r["id"], "prop", key)
        if m is None:
            raise Refusal("Tonight's race has no side bets.")
        if m["result"] is not None:
            raise Refusal(f"{db.label_of(m)} is already settled.")
        names = db.options_of(m)
        if pick not in names:
            raise Refusal(f"{pick} isn't in this race. Tonight: {', '.join(names)}.")
        ok, reason = db.place_bet(r["id"], m["id"], interaction.user.id, pick, amount)
        if not ok:
            raise Refusal(reason)
        callout = reason
        balance = db.wallet(interaction.user.id)["balance"]
        guild = getattr(interaction, "guild", None)
        slot = next((e["slot"] for e in db.entrants(r["id"]) if e["name"] == pick), None)
        icon = runner_icon(guild, pick, slot)
        await interaction.response.send_message(
            f"{amount:,} on {icon} **{pick}**, {market.name.lower()}. Pays "
            f"{economy.PROP_MULTIPLIER}x if you're right.\nYou have {balance:,} {CURRENCY} left.",
            ephemeral=True, view=undo_view(db.last_bet_id(interaction.user.id, m["id"])))
        await interaction.followup.send(
            callout or f"**{interaction.user.display_name}** bet {amount:,} on {icon} {pick}, "
                       f"{market.name.lower()}.")

    @app_commands.command(description="Everyone's bets on tonight's race: who's backing what.")
    async def bets(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        if r is None:
            raise Refusal("No race yet.")
        rows = db.all_bets(r["id"])
        if not rows:
            raise Refusal("No bets yet. `/bet` to be the first.")
        guild = getattr(interaction, "guild", None)
        slots = {e["name"]: e["slot"] for e in db.entrants(r["id"])}
        sections: dict[str, list[str]] = {}
        for b in rows:
            if b["kind"] == "slate":
                pick = with_icons(guild, r["id"], b["selection"])
            else:
                pick = f"{runner_icon(guild, b['selection'], slots.get(b['selection']))} {b['selection']}"
            amount = "free pick" if b.get("comeback") else f"{b['amount']:,}"
            sections.setdefault(b["label"], []).append(f"**{b['who']}** {amount}: {pick}")
        embed = discord.Embed(title=f"Bets on {r['week_label']}", color=0x1E7A46)
        for label, lines in list(sections.items())[:24]:
            text = "\n".join(lines)
            embed.add_field(name=label, value=text[:1000] + ("\n..." if len(text) > 1000 else ""),
                            inline=False)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="How tonight's runners have finished in past races.")
    async def form(self, interaction: discord.Interaction):
        r = db.active_race(interaction.guild_id) or db.latest_race(interaction.guild_id)
        if r is None:
            raise Refusal("No race yet.")
        names = [e["name"] for e in db.entrants(r["id"])]
        records = db.form(names, r["mode"], r["guild_id"])
        lines = [f"**{x}**: {form_line(records[x])}" for x in names]
        embed = discord.Embed(
            title=f"Form guide: {r['week_label']}",
            description=(f"Past {db.MODES[r['mode']]['label']} races, latest first.\n\n"
                         + "\n".join(lines)),
            color=0x1E7A46)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
                f"**Side bets.** `/sidebet` on first star, most minigames, most coins, or most ? tiles. "
                f"Pays {economy.PROP_MULTIPLIER}x. Minigames goes to whoever won the most "
                "minigames. Coins goes to whoever has the most coins when the race ends. "
                "The game's own bonus stars don't decide these. A tie refunds the bet.\n\n"
                "**Bonus questions** pop up during the race. Tap an answer on the post.\n\n"
                f"Everyone starts with {economy.STARTING_BALANCE:,}. You can bet as much of "
                "it as you like."),
            color=0xFFB114)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Your points, your place, and how tonight's bets are doing.")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=status_embed(interaction.guild_id, interaction.user.id,
                               interaction.user.display_name),
            ephemeral=True)

    @app_commands.command(description="What you can do here, and for the crew, the show-night checklist.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Mario Party Horse Racing", color=0xFFB114, description=(
            "Guess the finishing order with `/bet` and put points on it. You get your "
            "stake back times the number of places you got right.\n\n"
            "`/status` your points, place and tonight's bets\n"
            "`/bet` guess the order  ·  `/sidebet` first star, minigames, coins, ? tiles\n"
            "`/bets` everyone's bets  ·  `/form` how the runners have done before\n"
            "`/leaderboard` standings  ·  `/payouts` how it pays\n"
            "`/ad submit` send in a fake ad for the stream\n\n"
            "Every bet slip has an **Undo** button until betting locks. Bonus bets pop up "
            "during the race: tap an answer on the post. Out of points? Minigame bets "
            f"give you a free pick worth {economy.COMEBACK_PRIZE}."))
        if is_staff(interaction.user):
            embed.add_field(name="Crew: show night", inline=False, value=(
                "1. `/race create`: mode, week, runners. Any time before the show.\n"
                "2. `/show start` when you go live. Betting opens. Optional: `/bonus bet` "
                "with timer \"Until betting locks\" for first-to-the-bank style bets.\n"
                "3. `/show next` at each break. The race segment locks betting and posts "
                "the control panel: tap it for ? tiles, minigame wins, turns, ★ first star, "
                "and 🎮 to open a 60-second minigame bet.\n"
                "4. `/bonus bet` any time for a quick question. Settle it with the "
                "**Pay out** button on the post.\n"
                "5. `/race result`: the order and coins. Settles everything, refunds bonus "
                "bets nobody settled, posts the standings.\n\n"
                "Mistakes: Undo on the panel, **Delete** on a bonus post, `/race void` "
                "refunds a side bet. `/review-ads` for ads people send in."))
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
        if interaction.user.id not in {x["user_id"] for x in rows}:
            mine = db.wallet(interaction.user.id, interaction.user.display_name)
            place, players = db.rank_of(interaction.user.id)
            lines.append(f"\nYou: {db.ordinal(place)} of {players}, {mine['balance']:,}")
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

    race = app_commands.Group(name="race", description="Run the race.",
                              default_permissions=CREW_VISIBLE)
    season = app_commands.Group(name="season", description="The season standings.")

    @race.command(description="Set up this week's race: Mario Party (4 runners) or Mario Kart (up to 12).")
    @app_commands.describe(
        mode="Mario Party: 4 runners, with props. Mario Kart: 2 to 12 runners, no props",
        runners="The field in order, comma-separated, e.g. Mario, Luigi, Peach, Yoshi",
        game="Name shown on the board, e.g. Mario Party 3. Defaults to the mode's name",
        turns="Turns (Party) or laps (Kart) the game is set to")
    @app_commands.choices(mode=MODE_CHOICES)
    @staff_only()
    async def create(self, interaction: discord.Interaction,
                     mode: app_commands.Choice[str], week: str, runners: str,
                     game: str = "", turns: app_commands.Range[int, 1, 99] | None = None):
        rules = db.MODES[mode.value]
        names = [x.strip() for x in runners.split(",") if x.strip()]
        low, high = rules["runners"]
        if not low <= len(names) <= high:
            count = f"exactly {low}" if low == high else f"between {low} and {high}"
            raise Refusal(f"{rules['label']} needs {count} runners, separated by commas. "
                          f"You gave {len(names)}.")
        if len(set(x.lower() for x in names)) != len(names):
            raise Refusal("Every runner needs a different name.")
        if db.active_race(interaction.guild_id):
            raise Refusal("There's already a race in progress. Finish it first.")
        if db.current_season(interaction.guild_id) is None:
            db.start_season(interaction.guild_id, "Season 1")
        game = game.strip() or rules["label"]
        turns = turns or rules["turns"]
        db.create_race(interaction.guild_id, week, game, names, turns, mode=mode.value)
        props = "" if rules["props"] else " No props."
        await interaction.response.send_message(
            f"**{week}** is built. {game}, {turns} {rules['unit']}s, {len(names)} runners: "
            f"{', '.join(names)}.{props}\n"
            "`/show start` to run it off the rundown, or `/race open` by hand.")
        # After replying, so the crew isn't left waiting on Discord.
        await self.bot.apply_mode(mode.value)

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

    @race.command(description="Manual override: close the night if you settled things by hand.")
    @staff_only()
    async def finish(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        out = db.finish_race(r["id"])
        if "error" in out:
            raise Refusal(out["error"])
        await interaction.response.send_message(
            embed=night_embed(r["week_label"], out["finish"], out["guesses"]))

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
    @host_only("reset the economy")
    async def reset(self, interaction: discord.Interaction, name: str):
        if db.active_race(interaction.guild_id):
            raise Refusal("Finish the race in progress before resetting.")
        players = len(db.leaderboard(10_000))
        await interaction.response.send_message(
            f"This puts all {players} wallets back to {economy.STARTING_BALANCE:,} and starts "
            f"**{name}**. The current table is saved to `/season hall` first. Sure?",
            view=ConfirmReset(interaction.user.id, interaction.guild_id, name), ephemeral=True)

    @app_commands.command(description="Server host: give someone points, or take some back with a negative amount.")
    @app_commands.describe(user="Who gets the points",
                           amount="Points to give. A negative number takes points back",
                           reason="Shown in the announcement, e.g. won the fan art contest")
    @app_commands.default_permissions(manage_guild=True)
    @host_only("gift points")
    async def gift(self, interaction: discord.Interaction, user: discord.Member,
                   amount: app_commands.Range[int, -100_000, 100_000],
                   reason: app_commands.Range[str, 0, 100] = ""):
        if amount == 0:
            raise Refusal("Pick an amount other than zero.")
        reason = adrules.clean_text(reason, 100)
        change, balance = db.gift(user.id, user.display_name, amount, interaction.user.id, reason)
        if change == 0:
            raise Refusal(f"{user.display_name} has no points to take back.")
        if change > 0:
            text = f"**{interaction.user.display_name}** gave **{user.display_name}** {change:,} {CURRENCY}"
        else:
            text = (f"**{interaction.user.display_name}** took {-change:,} {CURRENCY} back "
                    f"from **{user.display_name}**")
        text += f": {reason}." if reason else "."
        text += f" They now have {balance:,}."
        if change != amount:
            text += f" (Asked for {amount:,}, but a balance can't go below zero.)"
        # Public on purpose: a leaderboard is only fun if everyone can see it's fair.
        await interaction.response.send_message(text)

    @app_commands.command(name="overlay-links",
                          description="Server host: the OBS addresses and sizes, to send to whoever streams.")
    @app_commands.default_permissions(manage_guild=True)
    @host_only("see the overlay links")
    async def overlay_links(self, interaction: discord.Interaction):
        rows = [f"**{name}** ({w} × {h}), {where}\n{overlay.public_url(name)}"
                for name, w, h, where in OVERLAY_SOURCES]
        await interaction.response.send_message(
            "Add each as a Browser source in OBS with this width and height. These contain "
            "your overlay key, so send them privately, never in a public channel.\n\n"
            + "\n\n".join(rows), ephemeral=True)

    @app_commands.command(description="Server host: download a copy of the whole database.")
    @app_commands.default_permissions(manage_guild=True)
    @host_only("download backups")
    async def backup(self, interaction: discord.Interaction):
        stamp = time.strftime("%Y-%m-%d-%H%M")
        path = Path(tempfile.mkdtemp()) / f"horserace-{stamp}.db"
        db.snapshot(path)
        await interaction.response.send_message(
            "Here's a full copy of the season: every wallet, race, bet and past season. "
            "Keep it somewhere safe.", file=discord.File(path), ephemeral=True)

    @app_commands.command(description="Post the crew control panel: tallies, turns, undo.")
    @app_commands.default_permissions(manage_events=True)
    @staff_only()
    async def panel(self, interaction: discord.Interaction):
        r = require_race(interaction.guild_id)
        await interaction.response.send_message(panel_text(r["id"]), view=panel_view(r["id"]))

    @app_commands.command(description="Count a ? tile or minigame win by hand.")
    @app_commands.default_permissions(manage_events=True)
    @app_commands.choices(what=TALLY_CHOICES)
    @app_commands.autocomplete(pick=entrant_options)
    @staff_only()
    async def tally(self, interaction: discord.Interaction, what: app_commands.Choice[str],
                    pick: str, delta: int = 1):
        r = require_race(interaction.guild_id)
        count = db.bump_tally(r["id"], pick, delta, what.value)
        await interaction.response.send_message(f"{pick} at {count} ({what.name}).", ephemeral=True)

    @app_commands.command(description="Top anyone who's broke back up to the rail floor.")
    @app_commands.default_permissions(manage_events=True)
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
    @app_commands.default_permissions(manage_events=True)
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

    show = app_commands.Group(name="show", description="Run the broadcast rundown.",
                              default_permissions=CREW_VISIBLE)

    def _moved(self, out: dict) -> str:
        seg = out["segment"]
        line = f"**{seg['name']}** ({out['index'] + 1} of {out['count']}) — {seg['minutes']} min."
        if out["action"] == "open":
            line += "\nBetting is now **open**."
        elif out["action"] == "lock":
            line += "\nBetting is now **locked**."
        if seg.get("kind") == "game":
            line += "\nThe race is on. The control panel is below."
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
        if out["segment"].get("kind") == "game":
            # The panel arrives on its own, right where the crew is working.
            await interaction.followup.send(panel_text(r["id"]), view=panel_view(r["id"]))

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

    bonus = app_commands.Group(name="bonus", description="Mid-game bonus questions.",
                               default_permissions=CREW_VISIBLE)

    @bonus.command(name="bet", description="Open a bonus bet: pick a type and a question. Answers fill themselves in.")
    @app_commands.describe(
        question="e.g. Will Luigi turn it around? or Who's first to land on the bank?",
        type="Pick a character (back up to 2), Yes or No (back one side), or Minigame winner (adds Draw at 8x)",
        timer="How long betting stays open. 'Until betting locks' for bets set up at the start",
        answers="Optional: your own answers, comma-separated, instead of the type's",
        pays="Optional multiplier. Leave blank for fair odds")
    @app_commands.choices(
        type=[app_commands.Choice(name=v["label"], value=k) for k, v in economy.BONUS_TYPES.items()],
        timer=[app_commands.Choice(name="60 seconds", value=60),
               app_commands.Choice(name="90 seconds", value=90),
               app_commands.Choice(name="3 minutes", value=180),
               app_commands.Choice(name="Until betting locks for the race", value=0)])
    @staff_only()
    async def bet(self, interaction: discord.Interaction, question: str,
                  type: app_commands.Choice[str], timer: app_commands.Choice[int] | None = None,
                  answers: str = "", pays: app_commands.Range[int, 2, 20] | None = None):
        r = require_race(interaction.guild_id)
        field = [e["name"] for e in db.entrants(r["id"])]
        kind = type.value
        if answers.strip():
            options = [o.strip() for o in answers.split(",") if o.strip()]
            kind = "yesno" if kind == "yesno" else "custom" if kind != "character" else kind
        elif kind == "yesno":
            options = ["Yes", "No"]
        elif kind == "minigame":
            options = field + [economy.DRAW]
        else:
            options = field
        if not 2 <= len(options) <= 20 or len({o.lower() for o in options}) != len(options):
            raise Refusal("Between 2 and 20 different answers, please.")
        seconds = (timer.value if timer is not None
                   else economy.BONUS_TYPES.get(type.value, {"seconds": 90})["seconds"]) or None
        await open_bonus_post(interaction, r["id"], question, kind, options, seconds, pays)

    @bonus.command(description="Settle a bonus question. Pays out immediately.")
    @app_commands.autocomplete(market=bonus_options, winner=bonus_answer_options)
    @staff_only()
    async def call(self, interaction: discord.Interaction, market: str, winner: str):
        require_race(interaction.guild_id)
        try:
            market_id = int(market)
        except ValueError:
            raise Refusal("Pick the bonus from the list.")
        db.close_bonus(market_id)                # paying out closes betting first
        out = db.call_market_id(market_id, winner)
        await interaction.response.send_message(call_text(out))
        await refresh_bonus_post(self.bot, market_id)

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
        await refresh_bonus_post(self.bot, market_id)


# --------------------------------------------------------------------- ads


def create_ad(user_id: int, name: str, crew: bool, headline: str, body: str = "",
              tag: str = "", accent: str = "", weight: int = 1,
              image: bytes | None = None) -> tuple[int | None, str]:
    """Add an ad. Crew ads go live; anyone else's wait for review.
    Returns (ad id or None, message for the person)."""
    headline = adrules.clean_text(headline, adrules.MAX_HEADLINE)
    if not headline:
        return None, "Your ad needs a headline."
    body = adrules.clean_text(body, adrules.MAX_BODY)
    colour = adrules.clean_accent(accent, len(db.ads()))
    if colour is None:
        return None, "Colour needs to be a hex code like #5865f2."
    image_type = None
    if image is not None:
        if len(image) > adrules.MAX_IMAGE_BYTES:
            return None, "That image is over 4 MB. Try a smaller one."
        image_type = adrules.sniff_image(image)
        if image_type is None:
            return None, "That file isn't a PNG, JPG, GIF or WebP image."
    # Every ad waits for review, crew ones included, so each one is approved
    # on its own merits before it can air.
    if crew:
        status, tag, weight = "pending", adrules.clean_text(tag, adrules.MAX_TAG), max(1, min(10, weight))
    else:
        waiting = [a for a in db.ads(status="pending", submitted_by=user_id)]
        if len(waiting) >= adrules.MAX_PENDING_PER_PERSON:
            return None, (f"You already have {len(waiting)} ads waiting for review. "
                          "Once the crew gets through those you can send more.")
        status, weight = "pending", 1
        tag = adrules.clean_text(f"made by {name}", adrules.MAX_TAG)
    ad_id = db.add_ad(headline=headline, body=body, tag=tag, accent=colour, weight=weight,
                      status=status, submitted_by=user_id, submitted_name=name,
                      image_blob=image, image_type=image_type)
    if crew:
        return ad_id, f"Ad #{ad_id} is in the review queue. Approve it with `/review-ads`."
    return ad_id, (f"Thanks {name}! Your ad \"{headline}\" is with the crew for review. "
                   "It goes into the rotation once it's approved.")


def ad_line(a) -> str:
    extras = []
    if a["has_upload"] or a["image_file"]:
        extras.append("image")
    if a["weight"] > 1:
        extras.append(f"weight {a['weight']}")
    if a["pinned"]:
        extras.append("pinned, every 4th ad")
    if a["submitted_name"] and a["submitted_name"] != "config/ads.json":
        extras.append(f"from {a['submitted_name']}")
    tail = f" ({', '.join(extras)})" if extras else ""
    return f"`#{a['id']}` {a['headline']}{tail}"


def _colour(hex_code: str) -> int:
    try:
        return int(hex_code.lstrip("#"), 16)
    except (ValueError, AttributeError):
        return 0xFFA81E


def review_card(a) -> tuple[discord.Embed, discord.File | None]:
    embed = discord.Embed(
        title=f"Ad #{a['id']} waiting for review",
        description=(f"**{a['headline']}**\n{a['body']}".strip()
                     + f"\n\nFrom {a['submitted_name']}. Corner tag: {a['tag'] or 'none'}"),
        color=_colour(a["accent"]))
    image = db.ad_image(a["id"])
    if image is None:
        return embed, None
    blob, media_type = image
    filename = f"ad-{a['id']}.{adrules.EXTENSIONS.get(media_type, 'png')}"
    embed.set_image(url=f"attachment://{filename}")
    return embed, discord.File(io.BytesIO(blob), filename=filename)


class AdReview(discord.ui.View):
    """Approve or reject one waiting ad."""

    def __init__(self, ad_id: int):
        super().__init__(timeout=600)
        self.ad_id = ad_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Crew only.", ephemeral=True)
        return False

    async def _next(self, interaction: discord.Interaction, done: str):
        self.stop()
        left = len(db.ads(status="pending"))
        more = f" {left} more waiting: run `/review-ads` again." if left else " Queue's empty."
        await interaction.response.edit_message(content=done + more, embed=None, view=None,
                                                attachments=[])

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        a = db.ad(self.ad_id)
        if a is None or not db.approve_ad(self.ad_id, interaction.user.id):
            await self._next(interaction, "Someone already dealt with that one.")
            return
        await self._next(interaction, f"Approved #{self.ad_id} \"{a['headline']}\". It's in the rotation.")

    @discord.ui.button(label="Approve and pin", style=discord.ButtonStyle.primary)
    async def approve_pin(self, interaction: discord.Interaction, button: discord.ui.Button):
        a = db.ad(self.ad_id)
        if a is None or not db.approve_ad(self.ad_id, interaction.user.id):
            await self._next(interaction, "Someone already dealt with that one.")
            return
        db.set_pinned(self.ad_id, True)
        await self._next(interaction, f"Approved and pinned #{self.ad_id} \"{a['headline']}\". "
                                      "It comes back every 4th ad.")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        a = db.ad(self.ad_id)
        if a is None or a["status"] != "pending":
            await self._next(interaction, "Someone already dealt with that one.")
            return
        db.remove_ad(self.ad_id)
        await self._next(interaction, f"Rejected and deleted #{self.ad_id}.")


async def ad_options(interaction: discord.Interaction, current: str):
    crew = is_staff(interaction.user)
    rows = db.ads() if crew else db.ads(submitted_by=interaction.user.id)
    out = []
    for a in rows:
        label = f"#{a['id']} {a['headline']}" + (" (waiting)" if a["status"] == "pending" else "")
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=str(a["id"])))
    return out[:25]


class Ads(commands.Cog):
    """The fake ad rotation, managed from Discord."""

    def __init__(self, bot: HorseRace):
        self.bot = bot

    ad = app_commands.Group(name="ad", description="The fake ads that run on stream.")

    @ad.command(name="submit", description="Send in a fake ad for the stream. The crew approves each one.")
    @app_commands.describe(
        headline="Big text, up to 60 characters",
        body="Smaller line underneath, up to 120 characters",
        image=f"Optional picture instead of text. Best at {adrules.IDEAL_SIZE}; PNG, JPG, GIF or WebP",
        tag="Crew only: small corner label. Everyone else's say who made it",
        accent="Stripe colour as a hex code, like #5865f2",
        weight="Crew only: how often it comes up, 1 to 10")
    async def submit(self, interaction: discord.Interaction,
                     headline: app_commands.Range[str, 1, 60],
                  body: app_commands.Range[str, 0, 120] = "",
                  image: discord.Attachment | None = None,
                  tag: app_commands.Range[str, 0, 40] = "",
                  accent: str = "",
                  weight: app_commands.Range[int, 1, 10] = 1):
        crew = is_staff(interaction.user)
        data = None
        if image is not None:
            if image.size > adrules.MAX_IMAGE_BYTES:
                raise Refusal("That image is over 4 MB. Try a smaller one.")
            # Read it now: Discord's links to uploaded files stop working after a while.
            data = await image.read()
        ad_id, message = create_ad(interaction.user.id, interaction.user.display_name, crew,
                                   headline, body, tag, accent, weight, data)
        if ad_id is None:
            raise Refusal(message)
        # Viewers' submissions are announced publicly so the crew sees them come in.
        await interaction.response.send_message(message, ephemeral=crew)

    @ad.command(description="Take an ad out of the rotation. Crew can remove any; you can remove yours.")
    @app_commands.autocomplete(ad=ad_options)
    async def remove(self, interaction: discord.Interaction, ad: str):
        try:
            a = db.ad(int(ad.lstrip("#")))
        except ValueError:
            a = None
        if a is None:
            raise Refusal("Pick the ad from the list.")
        if not is_staff(interaction.user) and a["submitted_by"] != interaction.user.id:
            raise Refusal("You can only remove ads you sent in.")
        db.remove_ad(a["id"])
        await interaction.response.send_message(
            f"Removed #{a['id']} \"{a['headline']}\". It drops off the stream within a few seconds.",
            ephemeral=True)

    @app_commands.command(name="review-ads", description="Crew: approve or reject ads people sent in.")
    @app_commands.default_permissions(manage_events=True)
    @staff_only()
    async def review(self, interaction: discord.Interaction):
        waiting = db.ads(status="pending")
        if not waiting:
            raise Refusal("No ads waiting for review.")
        embed, file = review_card(waiting[0])
        note = f"{len(waiting)} waiting. Oldest first."
        kwargs = {"file": file} if file else {}
        await interaction.response.send_message(note, embed=embed, view=AdReview(waiting[0]["id"]),
                                                ephemeral=True, **kwargs)

    @ad.command(description="See the ad rotation.")
    async def list(self, interaction: discord.Interaction):
        crew = is_staff(interaction.user)
        if crew:
            live, waiting = db.ads(status="live"), db.ads(status="pending")
            parts = ["**In the rotation**"] + ([ad_line(a) for a in live] or ["Nothing. The ad slot is hidden."])
            if waiting:
                parts += ["", f"**Waiting for review ({len(waiting)})**"] + [ad_line(a) for a in waiting]
                parts += ["", "`/review-ads` to go through them."]
        else:
            mine = db.ads(submitted_by=interaction.user.id)
            if not mine:
                raise Refusal("You haven't sent in any ads. `/ad submit` to make one.")
            parts = [ad_line(a) + (": live" if a["status"] == "live" else ": waiting for review")
                     for a in mine]
        await interaction.response.send_message("\n".join(parts)[:1900], ephemeral=True)


OVERLAY_SOURCES = [
    ("bug", 520, 440, "a top corner"),
    ("bonus", 1280, 460, "lower middle, hides itself until a bonus opens"),
    ("tote", 1920, 400, "along the bottom"),
    ("casters", 1100, 460, "beside the caster cams"),
    ("ads", 1280, 260, "wherever you run ad breaks"),
    ("standings", 900, 700, "the wrap-up scene"),
    ("winners", 1920, 1080, "full screen in the wrap-up scene; tick 'Refresh browser when "
                            "scene becomes active' to replay the reveal"),
]

# Every command group the bot loads. Tests build the bot from this same list.
COGS = (Betting, Broadcast, Show, Bonus, Ads)


async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, (Refusal, app_commands.CheckFailure)):
        message = str(original)
    elif isinstance(original, app_commands.CommandSignatureMismatch):
        message = ("The commands just changed for tonight's game. Press Ctrl+R to reload "
                   "Discord, then try again.")
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
