"""Opt-in per-server join-burst alerts, without automatic member punishment."""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from features import manager, NO_MENTIONS, require_owner

log = logging.getLogger("kuro.raid")
MAX_JOINS = 250
RETENTION_SECONDS = 600


class RaidGuard(commands.Cog):
    raid = app_commands.Group(
        name="raid", description="Non-punitive join burst monitoring", guild_only=True,
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.locks = {}

    def settings(self, guild_id):
        return self.store.option(guild_id, "raid_alert_config", {}) or {}

    def recent(self, guild_id):
        return self.store.option(guild_id, "raid_recent_joins", []) or []

    @raid.command(name="setup", description="Owner-only: alert on a sudden burst of member joins.")
    @manager
    async def setup(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
        threshold: app_commands.Range[int, 5, 50] = 8,
        window_seconds: app_commands.Range[int, 30, 300] = 60,
    ):
        require_owner(interaction)
        if not self.bot.intents.members:
            raise ValueError("Kuro requires the Server Members intent for join monitoring.")
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            raise ValueError("Kuro needs View Channel and Send Messages in that channel.")
        self.store.set_option(interaction.guild_id, "raid_alert_config", {
            "enabled": True, "channel_id": channel.id,
            "threshold": threshold, "window_seconds": window_seconds,
        })
        await interaction.response.send_message(
            "Raid-warning monitor enabled. Alerts go to " + channel.mention
            + ". Burst threshold: " + str(threshold)
            + " human joins in " + str(window_seconds)
            + " seconds. No members will be automatically punished.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @raid.command(name="status", description="Owner-only: report this server's join-burst settings.")
    @manager
    async def status(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        if not cfg.get("enabled"):
            await interaction.response.send_message(
                "Raid-warning monitor is disabled. Use /raid setup.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Raid alerts: enabled, observation-only"
            + "\nChannel: <#" + str(cfg["channel_id"]) + ">"
            + "\nThreshold: " + str(cfg["threshold"])
            + " member joins in " + str(cfg["window_seconds"]) + "s"
            + "\nRecent joins stored: " + str(len(self.recent(interaction.guild_id))),
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @raid.command(name="test", description="Owner-only: verify the raid alert channel.")
    @manager
    async def test(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        if not cfg.get("enabled"):
            raise ValueError("Enable /raid setup first.")
        channel = interaction.guild.get_channel(cfg["channel_id"])
        if channel is None:
            raise ValueError("Raid alert channel is missing; rerun /raid setup.")
        try:
            await channel.send(
                "🛡️ KURO RAID MONITOR TEST: alerts are working. "
                "No joins were simulated and no member was punished.",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as exc:
            raise ValueError("Could not deliver test alert. Check channel permissions.") from exc
        await interaction.response.send_message("Raid test alert sent.", ephemeral=True)

    @raid.command(name="disable", description="Owner-only: disable join-burst monitoring.")
    @manager
    async def disable(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        cfg["enabled"] = False
        self.store.set_option(interaction.guild_id, "raid_alert_config", cfg)
        await interaction.response.send_message(
            "Raid monitor disabled. Recent join records expire automatically.",
            ephemeral=True,
        )

    async def inspect_join(self, member):
        if member.bot:
            return
        guild = member.guild
        cfg = self.settings(guild.id)
        if not cfg.get("enabled"):
            return
        lock = self.locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            cfg = self.settings(guild.id)
            if not cfg.get("enabled"):
                return
            now = int(time.time())
            data = [
                event for event in self.recent(guild.id)
                if 0 <= now - event.get("at", 0) < RETENTION_SECONDS
            ]
            # The join event can be replayed when the bot reconnects.
            if any(event["user"] == member.id for event in data):
                return
            age_seconds = (discord.utils.utcnow() - member.created_at).total_seconds()
            new_account = age_seconds < 7 * 86400
            data.append({"at": now, "user": member.id, "young": new_account})
            self.store.set_option(guild.id, "raid_recent_joins", data[-MAX_JOINS:])

            active = [
                event for event in data
                if now - event["at"] <= cfg["window_seconds"]
            ]
            if len(active) < cfg["threshold"]:
                return
            last_alert = self.store.option(guild.id, "raid_last_alert", 0) or 0
            if now - last_alert < cfg["window_seconds"]:
                return
            channel = guild.get_channel(cfg["channel_id"])
            if channel is None:
                log.warning("Raid monitor alert channel missing in %s", guild.id)
                return
            young = sum(1 for event in active if event["young"])
            try:
                await channel.send(
                    "🚨 **KURO JOIN-BURST WARNING**\n"
                    + str(len(active)) + " unique human accounts joined within "
                    + str(cfg["window_seconds"]) + " seconds.\n"
                    + str(young) + " were created within the last seven days "
                    + "(a review signal, not proof of a raid).\n"
                    + "Response: alert only. No automatic kick, ban or lockdown.\n"
                    + "<@" + str(guild.owner_id)
                    + "> review invites, verification and member activity.",
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=guild.owner_id)],
                    ),
                )
            except discord.HTTPException:
                log.warning("Raid monitor alert delivery failed in %s", guild.id)
                return
            self.store.set_option(guild.id, "raid_last_alert", now)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            await self.inspect_join(member)
        except Exception:
            log.exception("Join-burst monitoring failed for guild %s", member.guild.id)
