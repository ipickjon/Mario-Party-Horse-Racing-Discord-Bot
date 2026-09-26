"""Fake Discord objects that drive the real command code.

Commands go through discord.py's own permission checks and invocation, and
errors go through the bot's real error handler, so what these tests see is
what a player in the server would see. The only thing not exercised is the
network connection to Discord itself.
"""

from __future__ import annotations

import types

from discord import app_commands

from mpr import bot as B


class Response:
    def __init__(self, outer):
        self.outer, self._done = outer, False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False,
                           file=None):
        self._done = True
        self.outer.sent.append({"content": content, "embed": embed, "view": view,
                                "ephemeral": ephemeral, "file": file})

    async def edit_message(self, *, content=None, embed=None, view=None, attachments=None):
        self._done = True
        self.outer.edits.append({"content": content, "embed": embed, "view": view})

    async def send_modal(self, modal):
        self._done = True
        self.outer.modals.append(modal)


class Message:
    def __init__(self):
        self.id, self.channel, self.edits = 777, types.SimpleNamespace(id=555), []

    async def edit(self, **kw):
        self.edits.append(kw)


class Interaction:
    def __init__(self, user_id=1, name="viewer", staff=False, host=False, guild_id=42,
                 namespace=None):
        self.user = types.SimpleNamespace(
            id=user_id, display_name=name,
            roles=[types.SimpleNamespace(name=B.STAFF_ROLE)] if staff else [],
            guild_permissions=types.SimpleNamespace(manage_guild=host))
        self.guild_id = guild_id
        self.command = None
        self.namespace = namespace or types.SimpleNamespace()
        self.sent, self.edits, self.modals = [], [], []
        self.response = Response(self)
        self.message = Message()
        self.followup = types.SimpleNamespace(send=self.response.send_message)

    async def original_response(self):
        return self.message

    @property
    def text(self) -> str:
        """Everything the user was shown, flattened for assertions."""
        parts = []
        for s in self.sent:
            if s["content"]:
                parts.append(s["content"])
            if s["embed"] is not None:
                e = s["embed"]
                parts += [e.title or "", e.description or ""]
                parts += [f.name + " " + f.value for f in e.fields]
        return "\n".join(parts)


async def build_bot():
    bot = B.HorseRace()
    for cog in B.COGS:
        await bot.add_cog(cog(bot))
    bot.install_mode("party")
    bot.syncs = []

    async def record_sync():
        bot.syncs.append(bot.mode)
    bot.sync_commands = record_sync
    bot.tree.on_error = B.on_error
    return bot


def find(bot, path: str) -> app_commands.Command:
    for cmd in bot.tree.walk_commands():
        if cmd.qualified_name == path and isinstance(cmd, app_commands.Command):
            return cmd
    raise KeyError(path)


async def run(bot, path: str, inter: Interaction, **kwargs) -> Interaction:
    """Invoke a slash command the way discord.py does after parsing."""
    cmd = find(bot, path)
    inter.command = cmd
    for key, value in list(kwargs.items()):
        param = next((p for p in cmd.parameters if p.name == key), None)
        if param is not None and param.choices and isinstance(value, str):
            choice = next(c for c in param.choices if c.name == value or c.value == value)
            kwargs[key] = choice
    try:
        if not await cmd._check_can_run(inter):
            raise app_commands.CheckFailure("check failed")
        await cmd._do_call(inter, kwargs)
    except app_commands.AppCommandError as error:
        await bot.tree.on_error(inter, error)
    return inter


def edited_text(inter: Interaction) -> str:
    parts = []
    for e in inter.edits:
        if e["content"]:
            parts.append(e["content"])
        if e["embed"] is not None:
            parts += [e["embed"].title or "", e["embed"].description or ""]
    return "\n".join(parts)


async def click(view, label: str, inter: Interaction) -> Interaction:
    """Click a button on an ordinary (non-persistent) view."""
    item = next(c for c in view.children if getattr(c, "label", None) == label)
    if await view.interaction_check(inter):
        await item.callback(inter)
    return inter
