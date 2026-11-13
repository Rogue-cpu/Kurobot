"""Kurobot native ticket system: private support channels with durable state.

This module deliberately does not import messages from third-party ticket bots,
delete tickets automatically, or share ticket contents with other servers.
"""
import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from features import NO_MENTIONS, manager, require_owner

log = logging.getLogger("kuro.tickets")


def ticket_name(member):
    name = re.sub(r"[^a-z0-9-]", "-", member.name.casefold()).strip("-")
    name = re.sub(r"-+", "-", name)[:30] or "member"
    return "ticket-" + name


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open a ticket", emoji="🎫",
        style=discord.ButtonStyle.primary, custom_id="kuro:tickets:open:v1",
    )
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                "Support tickets are temporarily unavailable.", ephemeral=True,
            )
            return
        await cog.open_ticket(interaction)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close ticket", emoji="🔒",
        style=discord.ButtonStyle.secondary, custom_id="kuro:tickets:close:v1",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                "The ticket system is temporarily unavailable.", ephemeral=True,
            )
            return
        await cog.close_ticket(interaction)


class Tickets(commands.Cog):
    tickets = app_commands.Group(
        name="tickets", description="Private staff support ticket system",
        guild_only=True,
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.locks = {}

    async def cog_load(self):
        # Global persistent custom IDs survive a bot restart.
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketCloseView())

    def settings(self, guild_id):
        return self.store.option(guild_id, "tickets_settings", {}) or {}

    def records(self, guild_id):
        return self.store.option(guild_id, "tickets_records", {}) or {}

    def _record_key(self, user_id):
        return str(user_id)

    @tickets.command(name="setup", description="Owner-only: configure private support tickets.")
    @manager
    async def setup(
        self, interaction: discord.Interaction,
        category: discord.CategoryChannel, staff_role: discord.Role,
        log_channel: discord.TextChannel,
    ):
        require_owner(interaction)
        guild = interaction.guild
        if category.guild.id != guild.id or staff_role.guild.id != guild.id:
            raise ValueError("Choose a category and staff role from this server.")
        if not guild.me.guild_permissions.manage_channels:
            raise ValueError("Kuro needs Manage Channels to create private tickets.")
        logperms = log_channel.permissions_for(guild.me)
        if not (logperms.view_channel and logperms.send_messages):
            raise ValueError("Kuro must be able to send messages in the ticket log channel.")
        if staff_role.is_default():
            raise ValueError("Choose a dedicated staff role, not @everyone.")
        self.store.set_option(guild.id, "tickets_settings", {
            "enabled": True,
            "category_id": category.id,
            "staff_role_id": staff_role.id,
            "log_channel_id": log_channel.id,
        })
        await interaction.response.send_message(
            "Private ticket system configured. Use /tickets panel to publish a "
            "public Open Ticket button. Ticket channels are visible only to "
            "the requester, chosen staff role, Kuro and applicable administrators.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @tickets.command(name="panel", description="Owner-only: publish an Open Ticket button.")
    @manager
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        if not cfg.get("enabled"):
            raise ValueError("Configure /tickets setup first.")
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ValueError("Kuro needs View Channel, Send Messages and Embed Links.")
        embed = discord.Embed(
            title="🎫 Apex Support Center",
            description="Need help from server staff? Press **Open a ticket**. "
            "Your request will create a private support channel.",
            color=discord.Color.from_rgb(187, 27, 50),
        )
        try:
            await channel.send(
                embed=embed, view=TicketPanelView(),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as exc:
            raise ValueError("Could not publish the ticket panel.") from exc
        await interaction.response.send_message(
            "Ticket panel published in " + channel.mention + ".",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @tickets.command(name="status", description="Owner-only: inspect ticket configuration and open records.")
    @manager
    async def status(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        rows = list(self.records(interaction.guild_id).values())
        await interaction.response.send_message(
            "Apex tickets: " + ("enabled" if cfg.get("enabled") else "disabled")
            + "\nRecorded open tickets: " + str(sum(r.get("status") == "open" for r in rows))
            + "\nTotal records: " + str(len(rows))
            + "\nStaff role: <@&" + str(cfg.get("staff_role_id", 0)) + ">"
            + "\nTicket category: <#" + str(cfg.get("category_id", 0)) + ">",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @tickets.command(name="disable", description="Owner-only: stop creation of new tickets.")
    @manager
    async def disable(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        cfg["enabled"] = False
        self.store.set_option(interaction.guild_id, "tickets_settings", cfg)
        await interaction.response.send_message(
            "New ticket creation disabled. Existing ticket channels remain untouched.",
            ephemeral=True,
        )

    async def open_ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Tickets require a server.", ephemeral=True)
            return
        member = interaction.user
        cfg = self.settings(guild.id)
        if not cfg.get("enabled"):
            await interaction.response.send_message(
                "Ticket creation is not enabled in this server.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        lock = self.locks.setdefault((guild.id, member.id), asyncio.Lock())
        async with lock:
            records = self.records(guild.id)
            key = self._record_key(member.id)
            existing = records.get(key)
            if existing and existing.get("status") == "open":
                channel = guild.get_channel(existing["channel_id"])
                if channel is not None:
                    await interaction.followup.send(
                        "You already have an open ticket: " + channel.mention,
                        ephemeral=True, allowed_mentions=NO_MENTIONS,
                    )
                    return
                # A deleted channel cannot block new support requests.
                existing["status"] = "missing"
                records[key] = existing
                self.store.set_option(guild.id, "tickets_records", records)
            category = guild.get_channel(cfg["category_id"])
            role = guild.get_role(cfg["staff_role_id"])
            if not isinstance(category, discord.CategoryChannel) or role is None:
                await interaction.followup.send(
                    "Ticket configuration is incomplete. Ask the owner to re-run /tickets setup.",
                    ephemeral=True,
                )
                return
            if not guild.me.guild_permissions.manage_channels:
                await interaction.followup.send(
                    "Kuro lacks Manage Channels for private tickets.", ephemeral=True,
                )
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
                role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True,
                ),
            }
            try:
                channel = await guild.create_text_channel(
                    name=ticket_name(member), category=category,
                    overwrites=overwrites,
                    reason="Apex support ticket created by requester " + str(member.id),
                )
            except (discord.HTTPException, discord.Forbidden):
                log.warning("Apex could not create private ticket for member %s", member.id)
                await interaction.followup.send(
                    "Discord refused to create the private ticket. Ask staff to check permissions.",
                    ephemeral=True,
                )
                return
            # Other users may have opened tickets while Discord was creating this
            # channel. Merge against the latest guild-wide record, not a stale
            # snapshot captured before awaiting create_text_channel().
            records = self.records(guild.id)
            records[key] = {
                "channel_id": channel.id,
                "user_id": member.id,
                "status": "open",
                "opened_at": int(time.time()),
            }
            self.store.set_option(guild.id, "tickets_records", records)
            try:
                await channel.send(
                    "🎫 **Apex Support Ticket**\n"
                    "A staff member will respond here. "
                    "Use the button below to close this ticket.",
                    view=TicketCloseView(), allowed_mentions=NO_MENTIONS,
                )
            except discord.HTTPException:
                log.warning("Ticket channel %s created but welcome notice failed", channel.id)
            await interaction.followup.send(
                "Your private support ticket is ready: " + channel.mention,
                ephemeral=True, allowed_mentions=NO_MENTIONS,
            )

    async def close_ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Tickets require a server.", ephemeral=True)
            return
        cfg = self.settings(guild.id)
        records = self.records(guild.id)
        entry = next(
            (r for r in records.values()
             if r.get("channel_id") == interaction.channel_id and r.get("status") == "open"),
            None,
        )
        if entry is None:
            await interaction.response.send_message(
                "This channel is not an open Apex ticket.", ephemeral=True,
            )
            return
        is_owner = interaction.user.id == entry["user_id"]
        is_staff = (
            guild.owner_id == interaction.user.id
            or any(role.id == cfg.get("staff_role_id") for role in getattr(interaction.user, "roles", []))
        )
        if not (is_owner or is_staff):
            await interaction.response.send_message(
                "Only the ticket requester or configured staff can close this ticket.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        lock = self.locks.setdefault((guild.id, entry["user_id"]), asyncio.Lock())
        async with lock:
            records = self.records(guild.id)
            current = records.get(self._record_key(entry["user_id"]))
            if not current or current.get("channel_id") != interaction.channel_id or current.get("status") != "open":
                await interaction.followup.send("This ticket is already closed.", ephemeral=True)
                return
            requester = guild.get_member(entry["user_id"])
            if requester is None:
                # Do not claim a successful close when a requester is uncached:
                # the existing user overwrite may still allow future posting.
                try:
                    requester = await guild.fetch_member(entry["user_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await interaction.followup.send(
                        "Cannot verify or lock the requester permissions. "
                        "The ticket remains open for staff to review.",
                        ephemeral=True,
                    )
                    return
            try:
                await interaction.channel.set_permissions(
                    requester, view_channel=True, send_messages=False,
                    read_message_history=True, reason="Apex ticket closed",
                )
            except discord.HTTPException:
                log.warning("Could not lock ticket %s for requester", interaction.channel_id)
                await interaction.followup.send(
                    "Unable to lock the ticket channel. Nothing was marked closed.",
                    ephemeral=True,
                )
                return
            # Merge after the awaited Discord permission change so concurrent
            # tickets belonging to other users are never silently overwritten.
            records = self.records(guild.id)
            current = records.get(self._record_key(entry["user_id"]))
            if not current or current.get("channel_id") != interaction.channel_id:
                await interaction.followup.send(
                    "Ticket state changed during closing. Please ask staff to review.",
                    ephemeral=True,
                )
                return
            current["status"] = "closed"
            current["closed_at"] = int(time.time())
            current["closed_by"] = interaction.user.id
            records[self._record_key(entry["user_id"])] = current
            self.store.set_option(guild.id, "tickets_records", records)
            try:
                await interaction.channel.send(
                    "🔒 This Apex support ticket has been closed. "
                    "The channel and its conversation are preserved for staff review.",
                    allowed_mentions=NO_MENTIONS,
                )
            except discord.HTTPException:
                pass
            log_channel = guild.get_channel(cfg.get("log_channel_id"))
            if log_channel:
                try:
                    await log_channel.send(
                        "Apex ticket closed | channel ID " + str(interaction.channel_id)
                        + " | requester ID " + str(entry["user_id"])
                        + " | closed by " + str(interaction.user.id),
                        allowed_mentions=NO_MENTIONS,
                    )
                except discord.HTTPException:
                    log.warning("Ticket closure log failed for guild %s", guild.id)
            await interaction.followup.send("Ticket closed. No messages were deleted.", ephemeral=True)
