"""Conservative, owner-controlled containment for audit-confirmed vanity changes.

This removes removable dangerous roles, not the member's account, and persists
exactly which roles were removed for later *manual* owner review.
"""
import logging
import time

import discord

log = logging.getLogger("kuro.vanity.containment")

DANGEROUS_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "moderate_members",
)


def dangerous_role(role):
    permissions = role.permissions
    return any(getattr(permissions, name, False) for name in DANGEROUS_PERMISSIONS)


def quarantine_key(member_id):
    return "vanity_quarantine:" + str(member_id)


class VanityContainment:
    def __init__(self, bot, store):
        self.bot = bot
        self.store = store

    def record(self, guild_id, member_id):
        return self.store.option(guild_id, quarantine_key(member_id), {}) or {}

    def risk_roles(self, guild):
        """Server-level dangerous permissions only. Channel-specific audits are separate."""
        return [
            role for role in guild.roles
            if getattr(role.permissions, "administrator", False)
            or getattr(role.permissions, "manage_guild", False)
        ]

    async def quarantine(self, guild, member_id, reason):
        """Contain only accounts below Kuro and only when verified by the caller."""
        if member_id in (guild.owner_id, self.bot.user.id):
            return "server owner or Kuro is exempt"
        if not guild.me.guild_permissions.manage_roles:
            return "Manage Roles missing; owner must contain manually"
        try:
            member = await guild.fetch_member(member_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "member not accessible; owner must investigate"
        if member.bot:
            return "bot account requires owner review and removal"
        if member.top_role >= guild.me.top_role:
            return "member's highest role is above or equal to Kuro; owner must act"
        # Evaluate *all* roles first. Discord role hierarchy can block some of them.
        danger = [role for role in member.roles if dangerous_role(role)]
        eligible = [
            role for role in danger
            if not role.is_default() and not role.managed and role < guild.me.top_role
        ]
        unreachable = [role for role in danger if role not in eligible]
        if not eligible:
            return ("dangerous roles cannot be removed; owner action required"
                    if unreachable else "member has no removable dangerous roles")

        key = quarantine_key(member_id)
        record = self.record(guild.id, member_id)
        record.setdefault("roles", [])
        record.setdefault("created", int(time.time()))
        record["member"] = member_id
        record["reason"] = reason[:300]
        record["status"] = "in_progress"
        self.store.set_option(guild.id, key, record)
        succeeded = []
        failed = []
        for role in eligible:
            try:
                await member.remove_roles(
                    role, reason="Kuro vanity guard: audit-confirmed unauthorized change",
                )
            except (discord.Forbidden, discord.HTTPException):
                failed.append(role.id)
                continue
            succeeded.append(role.id)
            # Save after each success; recovery survives a crash during containment.
            if role.id not in record["roles"]:
                record["roles"].append(role.id)
                self.store.set_option(guild.id, key, record)
        record["status"] = "contained" if not failed and not unreachable else "partial"
        record["failed"] = failed
        record["inaccessible"] = [role.id for role in unreachable]
        self.store.set_option(guild.id, key, record)
        summary = "removed " + str(len(succeeded)) + " dangerous role(s)"
        if failed or unreachable:
            summary += "; " + str(len(failed) + len(unreachable)) + " role(s) need owner attention"
        return summary + "; owner-only role restoration required"

    async def uphold(self, guild, member, added_roles):
        """Remove regranted privileged roles for an *already* quarantined user."""
        record = self.record(guild.id, member.id)
        if record.get("status") not in ("in_progress", "contained", "partial"):
            return None
        dangerous = [role for role in added_roles if dangerous_role(role)]
        if not dangerous:
            return None
        if member.id in (guild.owner_id, self.bot.user.id) or member.bot:
            return "Quarantine hold needs owner intervention (exempt account)."
        if not guild.me.guild_permissions.manage_roles or member.top_role >= guild.me.top_role:
            return "Quarantine hold cannot remove newly granted roles; fix Kuro role hierarchy."
        removed, failed = [], []
        for role in dangerous:
            if role.is_default() or role.managed or role >= guild.me.top_role:
                failed.append(role.id)
                continue
            try:
                await member.remove_roles(
                    role, reason="Kuro vanity quarantine hold: privileged role regrant",
                )
            except (discord.Forbidden, discord.HTTPException):
                failed.append(role.id)
                continue
            removed.append(role.id)
            if role.id not in record["roles"]:
                record["roles"].append(role.id)
                self.store.set_option(guild.id, quarantine_key(member.id), record)
        if failed:
            record["status"] = "partial"
            self.store.set_option(guild.id, quarantine_key(member.id), record)
        return (
            "Quarantine hold removed " + str(len(removed)) + " role(s); "
            + str(len(failed)) + " require owner attention."
        )

    async def restore(self, guild, member_id):
        """Called only after a confirm:true owner command. Never automatic."""
        record = self.record(guild.id, member_id)
        if not record.get("roles"):
            return "No Kuro-removed privileged roles are recorded for this member."
        if not guild.me.guild_permissions.manage_roles:
            raise ValueError("Kuro requires Manage Roles to restore privileges.")
        try:
            member = await guild.fetch_member(member_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            raise ValueError("Member is unavailable; original role records were retained.") from exc
        if member.top_role >= guild.me.top_role:
            raise ValueError("Member is above Kuro's role. Original role records were retained.")
        restored, failed, missing = [], [], []
        for role_id in list(record["roles"]):
            role = guild.get_role(role_id)
            if role is None or role.managed or role.is_default() or role >= guild.me.top_role:
                missing.append(role_id)
                continue
            if role in member.roles:
                restored.append(role_id)
                continue
            try:
                await member.add_roles(
                    role, reason="Kuro vanity guard: server owner explicitly approved role restoration",
                )
            except (discord.Forbidden, discord.HTTPException):
                failed.append(role_id)
                continue
            restored.append(role_id)
        # Keep records for anything not restored, including deleted/high roles.
        record["roles"] = [
            role_id for role_id in record["roles"] if role_id not in restored
        ]
        record["status"] = "restored" if not record["roles"] else "restore_incomplete"
        record["restored_at"] = int(time.time())
        self.store.set_option(guild.id, quarantine_key(member_id), record)
        return (
            "Restored " + str(len(restored)) + " role(s). "
            + str(len(missing) + len(failed)) + " unresolved and retained for review."
        )
