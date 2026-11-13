"""Owner-controlled vanity invite monitoring for Discord servers.

Bot accounts cannot reliably restore vanity codes with the supported Discord API.
This guard detects changes, alerts the owner, and can optionally time out an
attributed, moderatable actor. It never tries unsupported vanity edits.
"""
import asyncio
import logging
import time
from datetime import timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from features import NO_MENTIONS, manager, require_owner
from vanity_containment import VanityContainment

log = logging.getLogger("kuro.vanity")


class VanityGuard(commands.Cog):
    vanity = app_commands.Group(
        name="vanity", description="Protect and monitor this server's custom invite",
        guild_only=True,
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.locks = {}
        self.containment = VanityContainment(bot, store)

    async def cog_load(self):
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()

    def config(self, guild_id):
        return self.store.option(guild_id, "vanity_guard", {}) or {}

    async def current_code(self, guild):
        """Read actual live vanity state; fail closed if Discord denies access."""
        try:
            invite = await guild.vanity_invite()
        except discord.Forbidden as exc:
            raise ValueError("Kuro needs Manage Server to read this server's vanity invite.") from exc
        except discord.HTTPException as exc:
            raise ValueError("Discord could not return the vanity invite right now.") from exc
        return invite.code if invite is not None else None

    @vanity.command(name="setup", description="Guard the existing vanity code and send change alerts.")
    @manager
    async def setup(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
        timeout_minutes: app_commands.Range[int, 0, 60] = 0,
    ):
        require_owner(interaction)
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_guild:
            raise ValueError("Give Kuro Manage Server before enabling vanity protection.")
        perms = channel.permissions_for(guild.me)
        if not perms.view_channel or not perms.send_messages:
            raise ValueError("Kuro needs View Channel and Send Messages in the alert channel.")
        if timeout_minutes and not (
            guild.me.guild_permissions.view_audit_log
            and guild.me.guild_permissions.moderate_members
        ):
            raise ValueError("Automatic timeouts also require View Audit Log and Moderate Members.")
        code = await self.current_code(guild)
        if not code:
            raise ValueError("This server must have an active custom invite before it can be protected.")
        previous = self.config(guild.id)
        if previous.get("enabled") and previous.get("code") != code:
            raise ValueError("The current vanity differs from the protected baseline. Restore it in Discord or explicitly use /vanity approve.")
        self.store.set_option(guild.id, "vanity_guard", {
            "enabled": True,
            "code": code,
            "last_seen": code,
            "channel": channel.id,
            "timeout_minutes": timeout_minutes,
            "response": "timeout" if timeout_minutes else "alert",
        })
        await interaction.response.send_message(
            "Vanity guard enabled for discord.gg/" + code
            + ". Change alerts go to " + channel.mention
            + ". Auto-timeout: " + (str(timeout_minutes) + " minutes" if timeout_minutes else "off")
            + ". Kuro cannot automatically reclaim vanity codes; the owner must restore them in Discord.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @vanity.command(name="status", description="Compare the protected custom invite with Discord's live code.")
    @manager
    async def status(self, interaction: discord.Interaction):
        config = self.config(interaction.guild_id)
        if not config.get("enabled"):
            await interaction.response.send_message("Vanity guard is disabled. Use /vanity setup.", ephemeral=True)
            return
        code = await self.current_code(interaction.guild)
        expected = config["code"]
        state = "MATCH" if code == expected else "MISMATCH: owner action required"
        await interaction.response.send_message(
            "Vanity guard: " + state + "\nProtected: discord.gg/" + expected
            + "\nCurrent: " + ("discord.gg/" + code if code else "(unset)")
            + "\nAlert channel: <#" + str(config["channel"]) + ">"
            + "\nResponse: " + config.get("response", "timeout" if config.get("timeout_minutes", 0) else "alert")
            + "\nAutomatic timeout setting: " + str(config.get("timeout_minutes", 0)) + " minutes.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @vanity.command(name="enforcement", description="Owner-only: set minutes of verified-changer timeout, or 0 for alerts.")
    @manager
    async def enforcement(
        self, interaction: discord.Interaction,
        timeout_minutes: app_commands.Range[int, 0, 60],
    ):
        require_owner(interaction)
        config = self.config(interaction.guild_id)
        if not config.get("enabled"):
            raise ValueError("Enable vanity protection first.")
        perms = interaction.guild.me.guild_permissions
        if timeout_minutes and not (perms.view_audit_log and perms.moderate_members):
            raise ValueError("Automatic timeouts require View Audit Log and Moderate Members.")
        config["timeout_minutes"] = timeout_minutes
        config["response"] = "timeout" if timeout_minutes else "alert"
        self.store.set_option(interaction.guild_id, "vanity_guard", config)
        await interaction.response.send_message(
            "Vanity guard enforcement: " + (
                str(timeout_minutes) + "-minute timeout for an exactly audit-verified, "
                "moderatable changer." if timeout_minutes else "alert-only."
            ), ephemeral=True,
        )

    @vanity.command(name="response", description="Owner-only: select alert, timed timeout, or role quarantine.")
    @manager
    async def response(self, interaction: discord.Interaction, mode: Literal["alert", "timeout", "quarantine"]):
        require_owner(interaction)
        config = self.config(interaction.guild_id)
        if not config.get("enabled"):
            raise ValueError("Enable vanity protection first.")
        permissions = interaction.guild.me.guild_permissions
        if mode in ("quarantine", "timeout") and not permissions.view_audit_log:
            raise ValueError("Enforcement requires View Audit Log for exact attribution.")
        if mode == "quarantine" and not permissions.manage_roles:
            raise ValueError("Quarantine requires Manage Roles and a Kuro role above the offender's roles.")
        if mode == "timeout" and (not permissions.moderate_members or not config.get("timeout_minutes", 0)):
            raise ValueError("Configure /vanity enforcement timeout_minutes:10 and Moderate Members first.")
        config["response"] = mode
        self.store.set_option(interaction.guild_id, "vanity_guard", config)
        await interaction.response.send_message(
            "Vanity response set to " + mode + ". Only a recent, exactly-matching "
            "Discord audit entry can trigger automatic enforcement. "
            "Server owners and accounts above Kuro's role remain protected.",
            ephemeral=True,
        )

    @vanity.command(name="risk", description="Owner-only: list roles capable of changing server settings.")
    @manager
    async def risk(self, interaction: discord.Interaction):
        require_owner(interaction)
        roles = self.containment.risk_roles(interaction.guild)
        lines = [
            ("@everyone" if role.is_default() else role.name)
            + " (ID: " + str(role.id) + ")"
            + (" [Kuro cannot manage]" if role >= interaction.guild.me.top_role else "")
            for role in roles[-20:]
        ]
        await interaction.response.send_message(
            "Roles with Administrator or Manage Server (max 20 shown):\\n"
            + ("\\n".join(lines) if lines else "None found.")
            + "\\nThis audits server-level role permissions, not administrator account compromise.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @vanity.command(name="quarantine_status", description="Owner-only: review saved containment for a member.")
    @manager
    async def quarantine_status(self, interaction: discord.Interaction, member: discord.User):
        require_owner(interaction)
        record = self.containment.record(interaction.guild_id, member.id)
        await interaction.response.send_message(
            ("Status: " + record.get("status", "not recorded")
             + "\\nRemoved dangerous role IDs: " + (", ".join(map(str, record.get("roles", []))) or "none")
             + "\\nReason: " + record.get("reason", "n/a")),
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @vanity.command(name="quarantine_restore", description="Owner-only: explicitly restore saved removed roles.")
    @manager
    async def quarantine_restore(self, interaction: discord.Interaction, member: discord.User, confirm: bool = False):
        require_owner(interaction)
        if not confirm:
            await interaction.response.send_message(
                "No roles restored. Re-run with confirm:true ONLY if you verified "
                "the account is secure. This can regrant Administrator or Manage Server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.containment.restore(interaction.guild, member.id)
        await interaction.followup.send(result, ephemeral=True)

    @vanity.command(name="approve", description="Owner-only: accept the current invite as the new protected code.")
    @manager
    async def approve(self, interaction: discord.Interaction):
        require_owner(interaction)
        config = self.config(interaction.guild_id)
        if not config.get("enabled"):
            raise ValueError("Enable vanity protection first with /vanity setup.")
        code = await self.current_code(interaction.guild)
        if not code:
            raise ValueError("There is no live vanity invite to approve. Restore it in Discord first.")
        config["code"] = code
        config["last_seen"] = code
        self.store.set_option(interaction.guild_id, "vanity_guard", config)
        await interaction.response.send_message(
            "Owner-approved vanity baseline: discord.gg/" + code
            + ". Previous incidents remain in the record.", ephemeral=True,
        )

    @vanity.command(name="disable", description="Owner-only: stop vanity monitoring in this server.")
    @manager
    async def disable(self, interaction: discord.Interaction):
        require_owner(interaction)
        config = self.config(interaction.guild_id)
        config["enabled"] = False
        self.store.set_option(interaction.guild_id, "vanity_guard", config)
        await interaction.response.send_message("Vanity guard disabled.", ephemeral=True)

    @vanity.command(name="test", description="Owner-only: verify that Kuro can send an alert.")
    @manager
    async def test(self, interaction: discord.Interaction):
        require_owner(interaction)
        config = self.config(interaction.guild_id)
        if not config.get("enabled"):
            raise ValueError("Enable vanity protection first.")
        channel = interaction.guild.get_channel(config["channel"])
        if not channel:
            raise ValueError("The alert channel was deleted. Reconfigure /vanity setup.")
        try:
            await channel.send(
                "Kuro vanity guard test: alerts are working. "
                "No vanity change or punishment occurred.",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as exc:
            raise ValueError("Kuro cannot send to that channel. Check its permissions.") from exc
        await interaction.response.send_message("Test alert sent.", ephemeral=True)

    @vanity.command(name="incidents", description="Owner-only: show the latest vanity change records.")
    @manager
    async def incidents(self, interaction: discord.Interaction):
        require_owner(interaction)
        events = self.store.option(interaction.guild_id, "vanity_incidents", []) or []
        lines = []
        for event in events[-5:]:
            actor = str(event["actor"]) if event.get("actor") else "unconfirmed"
            lines.append(
                "<t:" + str(event["at"]) + ":f> | "
                + str(event["previous"]) + " -> " + str(event["current"])
                + " | Actor: " + actor + " | " + event["outcome"]
            )
        await interaction.response.send_message(
            "\n".join(lines) or "No vanity incidents recorded.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    async def match_audit_actor(self, guild, expected, observed):
        """Only attribute a change when both audit diff values match exactly."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(
                limit=12, action=discord.AuditLogAction.guild_update,
            ):
                if (discord.utils.utcnow() - entry.created_at).total_seconds() > 75:
                    break
                before = getattr(entry.before, "vanity_url_code", object())
                after = getattr(entry.after, "vanity_url_code", object())
                if before == expected and after == observed and entry.user:
                    return entry.user.id
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Audit lookup unavailable for vanity incident in %s", guild.id)
        return None

    async def timeout_actor(self, guild, actor_id, minutes):
        if not minutes or not actor_id:
            return "alert only"
        if actor_id in (guild.owner_id, self.bot.user.id):
            return "protected owner or bot; no action"
        if not guild.me.guild_permissions.moderate_members:
            return "missing Moderate Members; no action"
        try:
            member = await guild.fetch_member(actor_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "actor no longer accessible; no action"
        if (member.bot or member.guild_permissions.administrator
                or member.top_role >= guild.me.top_role):
            return "actor is exempt or outside Kuro's role hierarchy; no action"
        try:
            until = discord.utils.utcnow() + timedelta(minutes=minutes)
            if member.timed_out_until and member.timed_out_until > until:
                return "existing longer timeout preserved"
            await member.timeout(
                until, reason="Kuro: verified unauthorized vanity change",
            )
            return "timed out for " + str(minutes) + " minutes"
        except discord.HTTPException:
            log.warning("Could not timeout vanity changer %s in %s", actor_id, guild.id)
            return "timeout failed; review permissions"

    async def inspect(self, guild, observed, source):
        lock = self.locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            config = self.config(guild.id)
            if not config.get("enabled") or config.get("last_seen") == observed:
                return
            expected = config["code"]
            channel = guild.get_channel(config["channel"])
            if observed == expected:
                config["last_seen"] = expected
                self.store.set_option(guild.id, "vanity_guard", config)
                if channel:
                    try:
                        await channel.send(
                            "Kuro vanity guard: the protected code has been restored to discord.gg/"
                            + expected + ".", allowed_mentions=NO_MENTIONS,
                        )
                    except discord.HTTPException:
                        log.warning("Vanity restoration alert failed for %s", guild.id)
                return
            # Polling can discover offline changes. Never punish retroactively:
            # enforce only for a live gateway event with an exact audit diff.
            actor = None
            previous_seen = config.get("last_seen", expected)
            if source == "gateway":
                await asyncio.sleep(1.5)
                actor = await self.match_audit_actor(guild, previous_seen, observed)
            mode = config.get("response", "timeout" if config.get("timeout_minutes", 0) else "alert")
            outcome = "pending " + mode if actor and mode != "alert" else "alert only"
            owner = guild.owner_id
            message = (
                "🚨 **KURO VANITY GUARD: UNAPPROVED CHANGE**\n"
                "Protected: discord.gg/" + expected
                + "\nObserved: " + ("discord.gg/" + observed if observed else "(removed)")
                + "\nActor: " + (str(actor) + " (audit-confirmed)" if actor else "not confirmed")
                + "\nResponse: " + outcome
                + "\n<@" + str(owner) + ">: review Server Settings and the audit log. "
                "Restore the vanity manually, or use /vanity approve if this was intentional."
            )
            if not channel:
                log.error("Vanity change in %s; alert channel %s is missing", guild.id, config["channel"])
                return
            try:
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=owner)]),
                )
            except discord.HTTPException:
                log.error("Could not send vanity alert in %s", guild.id)
                return
            if actor and source == "gateway" and mode != "alert":
                if mode == "quarantine":
                    outcome = await self.containment.quarantine(
                        guild, actor, "Audit-confirmed vanity change: " + str(previous_seen)
                        + " -> " + str(observed),
                    )
                elif mode == "timeout":
                    outcome = await self.timeout_actor(
                        guild, actor, config.get("timeout_minutes", 0),
                    )
                else:
                    outcome = "unsupported response configuration; alert only"
                try:
                    await channel.send(
                        "Kuro vanity enforcement result for user ID " + str(actor)
                        + ": " + outcome, allowed_mentions=NO_MENTIONS,
                    )
                except discord.HTTPException:
                    log.warning("Could not send vanity enforcement result in %s", guild.id)
            config["last_seen"] = observed
            self.store.set_option(guild.id, "vanity_guard", config)
            events = self.store.option(guild.id, "vanity_incidents", []) or []
            events.append({
                "at": int(time.time()), "previous": expected,
                "current": observed, "actor": actor, "outcome": outcome,
            })
            self.store.set_option(guild.id, "vanity_incidents", events[-25:])

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        if not self.config(after.id).get("enabled"):
            return
        if before.vanity_url_code == after.vanity_url_code:
            return
        try:
            actual = await self.current_code(after)
        except ValueError:
            log.warning("Cannot verify vanity event in %s", after.id)
            return
        await self.inspect(after, actual, "gateway")

    @tasks.loop(minutes=2)
    async def poll(self):
        for guild in self.bot.guilds:
            if not self.config(guild.id).get("enabled"):
                continue
            try:
                actual = await self.current_code(guild)
                await self.inspect(guild, actual, "poll")
            except ValueError:
                log.warning("Vanity polling unavailable for %s", guild.id)
            except Exception:
                log.exception("Vanity monitoring error for %s", guild.id)

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
