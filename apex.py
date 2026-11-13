"""Read-only owner inventory to plan replacement of other Discord bots.

Never changes, removes, demotes, or messages third-party integrations.
Information is limited to the guild whose owner invoked the command.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from features import NO_MENTIONS, manager, require_owner

log = logging.getLogger("kuro.apex")

MAX_SCAN = 1001
MAX_DISPLAY = 20
PRIVILEGE_LABELS = (
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("manage_webhooks", "Manage Webhooks"),
    ("ban_members", "Ban Members"),
)


def privilege_labels(member):
    permissions = member.guild_permissions
    if permissions.administrator:
        return ["Administrator"]
    return [
        label for key, label in PRIVILEGE_LABELS
        if getattr(permissions, key, False)
    ]


def bot_inventory_line(member):
    name = discord.utils.escape_markdown(member.display_name[:45])
    powers = privilege_labels(member)
    # Discord mentions remain disabled; use IDs rather than @user references.
    return (
        name + " (" + str(member.id) + ")"
        + " | " + (", ".join(powers) if powers else "no elevated server permissions")
    )


class Apex(commands.Cog):
    apex = app_commands.Group(
        name="apex",
        description="Read-only audit for safely replacing overlapping server bots",
        guild_only=True,
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store

    @apex.command(name="inventory", description="Owner-only: list existing bot accounts and their privileges.")
    @manager
    async def inventory(self, interaction: discord.Interaction):
        require_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not self.bot.intents.members:
            raise ValueError(
                "Kuro needs Server Members Intent to make a server bot inventory."
            )
        bots = []
        scanned = 0
        try:
            async for member in guild.fetch_members(limit=MAX_SCAN):
                scanned += 1
                if member.bot:
                    bots.append(member)
        except (discord.HTTPException, discord.Forbidden) as exc:
            raise ValueError(
                "Discord could not provide the guild member list. Check Server Members Intent."
            ) from exc
        bots.sort(key=lambda member: (member.id != self.bot.user.id, member.id))
        lines = [
            "KURO APEX BOT INVENTORY (READ-ONLY)",
            "Server ID: " + str(guild.id),
            "Accounts scanned: " + str(scanned)
            + (" (SCAN LIMIT: partial inventory)" if scanned >= MAX_SCAN else ""),
            "Bot accounts found: " + str(len(bots)),
            "",
        ]
        for member in bots[:MAX_DISPLAY]:
            line = bot_inventory_line(member)
            if member.id == self.bot.user.id:
                line += " [Kurobot]"
            lines.append(line)
        if len(bots) > MAX_DISPLAY:
            lines.append("... and " + str(len(bots) - MAX_DISPLAY) + " more bot accounts.")
        lines += [
            "",
            "This lists account permissions, not which features each bot provides.",
            "No bot was modified or removed. Compare each bot's actual commands "
            "and configuration before retiring it.",
        ]
        await interaction.followup.send(
            "\n".join(lines)[:1950], ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @apex.command(name="readiness", description="Owner-only: check Kuro modules installed and enabled.")
    @manager
    async def readiness(self, interaction: discord.Interaction):
        require_owner(interaction)
        guild_id = interaction.guild_id
        installed = {
            "Core features": self.bot.get_cog("Features") is not None,
            "Moderation/community": self.bot.get_cog("Wishlist") is not None,
            "Backups": self.bot.get_cog("Backups") is not None,
            "Vanity Guard": self.bot.get_cog("VanityGuard") is not None,
            "Anti-nuke alerts": self.bot.get_cog("AntiNuke") is not None,
            "Raid alerts": self.bot.get_cog("RaidGuard") is not None,
        }
        vanity = self.store.option(guild_id, "vanity_guard", {}) or {}
        antinuke = self.store.option(guild_id, "antinuke_config", {}) or {}
        raid = self.store.option(guild_id, "raid_alert_config", {}) or {}
        configured = {
            "Vanity Guard": "on (" + vanity.get("response", "alert") + ")" if vanity.get("enabled") else "off",
            "Anti-nuke alerts": "on" if antinuke.get("enabled") else "off",
            "Raid alerts": "on" if raid.get("enabled") else "off",
        }
        body = ["KURO APEX READINESS", "Installed modules:"]
        body += [("✓ " if enabled else "✗ ") + name for name, enabled in installed.items()]
        body += ["", "Active protection in this server:"]
        body += [name + ": " + state for name, state in configured.items()]
        body += [
            "",
            "Installed does not mean live-tested or feature-equivalent to other bots.",
            "Never remove third-party bots before confirming equivalent workflows.",
        ]
        await interaction.response.send_message(
            "\n".join(body), ephemeral=True, allowed_mentions=NO_MENTIONS,
        )
