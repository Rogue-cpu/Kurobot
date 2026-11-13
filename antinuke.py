"""Owner-controlled audit-log security alerts for destructive Discord activity.

No automatic punishment, no unverified attribution, and no unsupported rollback.
Designed as an independent opt-in anti-nuke monitor for authorized servers.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from features import NO_MENTIONS, manager, require_owner

log = logging.getLogger("kuro.antinuke")

EVENT_TYPES = {
    discord.AuditLogAction.channel_delete: ("channel deleted", "destructive"),
    discord.AuditLogAction.role_delete: ("role deleted", "destructive"),
    discord.AuditLogAction.webhook_delete: ("webhook deleted", "destructive"),
    discord.AuditLogAction.ban: ("member banned", "destructive"),
    discord.AuditLogAction.kick: ("member kicked", "destructive"),
    discord.AuditLogAction.channel_create: ("channel created", "structural"),
    discord.AuditLogAction.role_create: ("role created", "structural"),
    discord.AuditLogAction.webhook_create: ("webhook created", "sensitive"),
    discord.AuditLogAction.bot_add: ("bot added", "sensitive"),
}

RETENTION_SECONDS = 86400
MAX_EVENTS = 200
MAX_ALERT_ACTORS = 100


class AntiNuke(commands.Cog):
    antinuke = app_commands.Group(
        name="antinuke",
        description="Audit-log monitoring and early warning for server destruction",
        guild_only=True,
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.locks = {}

    def settings(self, guild_id):
        return self.store.option(guild_id, "antinuke_config", {}) or {}

    def events(self, guild_id):
        return self.store.option(guild_id, "antinuke_events", []) or []

    @antinuke.command(name="setup", description="Owner-only: enable anti-nuke alerts without punishment.")
    @manager
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        threshold: app_commands.Range[int, 2, 20] = 4,
        window_seconds: app_commands.Range[int, 30, 300] = 60,
    ):
        require_owner(interaction)
        guild = interaction.guild
        if not guild.me.guild_permissions.view_audit_log:
            raise ValueError("Kuro needs View Audit Log permission to monitor destructive changes.")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            raise ValueError("Kuro needs View Channel and Send Messages in the alert channel.")
        config = {
            "enabled": True,
            "channel_id": channel.id,
            "threshold": threshold,
            "window_seconds": window_seconds,
        }
        self.store.set_option(guild.id, "antinuke_config", config)
        await interaction.response.send_message(
            "Anti-nuke monitoring enabled for this server. "
            "Alerts go to " + channel.mention
            + ". Threshold: " + str(threshold)
            + " audited events by one actor in " + str(window_seconds)
            + " seconds. Webhook creation and bot additions alert immediately. "
            "**Alert-only:** no roles will be removed and no users will be punished.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @antinuke.command(name="status", description="Show this server's anti-nuke monitoring settings.")
    @manager
    async def status(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        if not cfg.get("enabled"):
            await interaction.response.send_message(
                "Anti-nuke monitoring is disabled. Use /antinuke setup.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Anti-nuke: enabled (alerts only)"
            + "\nAlert channel: <#" + str(cfg["channel_id"]) + ">"
            + "\nThreshold: " + str(cfg["threshold"])
            + " events in " + str(cfg["window_seconds"]) + " seconds"
            + "\nRetained audit events: " + str(len(self.events(interaction.guild_id)))
            + "\nRequires: View Audit Log + moderation intent.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @antinuke.command(name="test", description="Owner-only: send a harmless alert delivery test.")
    @manager
    async def test(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        if not cfg.get("enabled"):
            raise ValueError("Enable monitoring with /antinuke setup first.")
        channel = interaction.guild.get_channel(cfg["channel_id"])
        if not channel:
            raise ValueError("Alert channel missing. Run /antinuke setup to choose another.")
        try:
            await channel.send(
                "🛡️ KURO ANTI-NUKE TEST: alert delivery confirmed. "
                "No real security event, punishment or server change occurred.",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as exc:
            raise ValueError("Could not send the test alert. Check channel permissions.") from exc
        await interaction.response.send_message("Anti-nuke test alert sent.", ephemeral=True)

    @antinuke.command(name="incidents", description="Owner-only: review recent audited security events.")
    @manager
    async def incidents(self, interaction: discord.Interaction):
        require_owner(interaction)
        records = self.events(interaction.guild_id)[-8:]
        lines = [
            "<t:" + str(item["at"]) + ":R> "
            + item["type"]
            + " | actor " + str(item["actor"] if item["actor"] is not None else "unknown")
            + " | entry " + str(item["id"])
            for item in reversed(records)
        ]
        await interaction.response.send_message(
            "\n".join(lines) or "No monitored audit events recorded.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @antinuke.command(name="disable", description="Owner-only: stop anti-nuke monitoring.")
    @manager
    async def disable(self, interaction: discord.Interaction):
        require_owner(interaction)
        cfg = self.settings(interaction.guild_id)
        cfg["enabled"] = False
        self.store.set_option(interaction.guild_id, "antinuke_config", cfg)
        await interaction.response.send_message(
            "Anti-nuke monitoring disabled. Existing incident records retained.",
            ephemeral=True,
        )

    async def inspect_entry(self, entry):
        guild = entry.guild
        if guild is None:
            return
        cfg = self.settings(guild.id)
        if not cfg.get("enabled"):
            return
        event = EVENT_TYPES.get(entry.action)
        if event is None:
            return
        if not guild.me.guild_permissions.view_audit_log:
            log.warning("Anti-nuke audit permission missing in %s", guild.id)
            return
        actor = entry.user_id
        if actor == getattr(self.bot.user, "id", None):
            # The bot's own recovery and setup actions should not self-trigger.
            return
        lock = self.locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            cfg = self.settings(guild.id)
            if not cfg.get("enabled"):
                return
            now = int(time.time())
            recent = [
                item for item in self.events(guild.id)
                if now - item.get("at", 0) < RETENTION_SECONDS
            ]
            # Audit entry IDs make reconnect/gateway duplicate processing safe.
            if any(item["id"] == entry.id for item in recent):
                return
            target_id = getattr(entry.target, "id", None) if entry.target is not None else None
            record = {
                "id": entry.id,
                "at": now,
                "actor": actor,
                "target": target_id,
                "type": event[0],
                "category": event[1],
            }
            recent.append(record)
            self.store.set_option(guild.id, "antinuke_events", recent[-MAX_EVENTS:])

            if actor is None:
                # Without a real audit actor we cannot group activity by user.
                # Still raise a warning for particularly sensitive single events.
                count = 0
            else:
                count = sum(
                    1 for item in recent
                    if item["actor"] == actor
                    and now - item["at"] <= cfg["window_seconds"]
                )
            urgent = event[1] == "sensitive"
            if not urgent and count < cfg["threshold"]:
                return

            previous = self.store.option(guild.id, "antinuke_last_alert", {}) or {}
            key = str(actor) if actor is not None else "unknown:" + event[0]
            last = int(previous.get(key, 0))
            if now - last < cfg["window_seconds"]:
                return

            channel = guild.get_channel(cfg["channel_id"])
            if channel is None:
                log.error("Anti-nuke alert channel missing in %s", guild.id)
                return
            message = (
                "🚨 **KURO ANTI-NUKE SECURITY ALERT**\n"
                "Latest: " + event[0]
                + "\nActor ID: " + (str(actor) if actor is not None else "unavailable")
                + "\nTarget ID: " + (str(target_id) if target_id else "not provided")
                + "\nRecent audited events by this actor: " + str(count)
                + " / " + str(cfg["threshold"])
                + " within " + str(cfg["window_seconds"]) + "s"
                + "\nResponse: **alert only**. No punishments, bans, "
                "role removals or lockdown were triggered."
                + "\n<@" + str(guild.owner_id)
                + "> review audit logs and use /protection lock manually if warranted."
            )
            try:
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=guild.owner_id)],
                    ),
                )
            except discord.HTTPException:
                log.warning("Anti-nuke alert delivery failed in %s", guild.id)
                return

            previous[key] = now
            # Keep only recent alert keys; database does not grow indefinitely.
            previous = {
                a: timestamp
                for a, timestamp in previous.items()
                if now - int(timestamp) < RETENTION_SECONDS
            }
            previous = dict(sorted(
                previous.items(), key=lambda pair: pair[1], reverse=True
            )[:MAX_ALERT_ACTORS])
            self.store.set_option(guild.id, "antinuke_last_alert", previous)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        try:
            await self.inspect_entry(entry)
        except Exception:
            log.exception("Anti-nuke monitor failed for audit event %s", getattr(entry, "id", "?"))
