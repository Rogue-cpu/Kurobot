"""Optional moderation, activity, recovery, and owner tools."""
import asyncio
import io
import json
import logging
import secrets
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from features import NO_MENTIONS, manager, require_owner
from storage import duration_seconds
from wishlist_store import WishlistStore

log = logging.getLogger("kuro.wishlist")


def staff(callback):
    callback = app_commands.guild_only()(callback)
    callback = app_commands.default_permissions(moderate_members=True)(callback)
    return app_commands.checks.has_permissions(moderate_members=True)(callback)


def ordinary_role(features, guild, role):
    return features.safe_role(role, guild) and role.id not in {
        (features.store.option(guild.id, "verification") or {}).get("role"),
        (features.store.option(guild.id, "jail") or {}).get("role"),
    }


def moderation_target(guild, actor, target):
    if target.id in (guild.owner_id, guild.me.id, actor.id) or target.bot:
        raise ValueError("Choose an ordinary member other than yourself.")
    if target.guild_permissions.administrator or target.top_role >= guild.me.top_role:
        raise ValueError("Kuro cannot moderate that member's role.")
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        raise ValueError("That member must be below your highest role.")


class BlueprintConfirm(discord.ui.View):
    def __init__(self, cog, code, owner):
        super().__init__(timeout=90)
        self.cog, self.code, self.owner = cog, code, owner
        self.used = False

    @discord.ui.button(label="Add this blueprint", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.owner or interaction.guild.owner_id != self.owner:
            await interaction.response.send_message("Only the current server owner can confirm.", ephemeral=True)
            return
        if self.used:
            await interaction.response.send_message("This import was already queued.", ephemeral=True)
            return
        try:
            key = self.cog.core.consume_invite("blueprint", self.code)
            row = self.cog.db.execute("SELECT payload FROM blueprints WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("That blueprint no longer exists.")
            payload = json.loads(row[0])
            payload.update({"role_map": {str(payload["source"]): interaction.guild_id}, "channel_map": {}, "role_step": 0, "channel_step": 0})
            for old,bot_id in payload.get("bot_roles",{}).items():
                existing=next((role for role in getattr(interaction.guild,"roles",[]) if role.tags and role.tags.bot_id==bot_id),None)
                if existing:
                    payload["role_map"][old]=existing.id
            with self.cog.db:
                cursor = self.cog.db.execute("INSERT INTO blueprint_jobs(guild,actor,payload,status) VALUES (?,?,?,'queued')", (interaction.guild_id, self.owner, json.dumps(payload)))
            self.used = True
            self.stop()
            await interaction.response.edit_message(content=f"Blueprint job #{cursor.lastrowid} queued. It adds structure without deleting existing channels. Use /blueprint status for progress.", view=None)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class Wishlist(commands.Cog):
    jail = app_commands.Group(name="jail", description="Timed quarantine and jail labels", guild_only=True)
    levels = app_commands.Group(name="levels", description="Activity XP and role rewards", guild_only=True)
    watch = app_commands.Group(name="watch", description="Invited server membership rules", guild_only=True)
    verification_policy = app_commands.Group(name="verification_policy", description="Account checks and moderator review", guild_only=True)
    recovery_plus = app_commands.Group(name="recovery_plus", description="Role and forum thread backups", guild_only=True)
    blueprint = app_commands.Group(name="blueprint", description="Copy server structure with both owners' consent", guild_only=True)
    protection = app_commands.Group(name="protection", description="Owner alerts and reversible lockdown", guild_only=True)
    tools = app_commands.Group(name="tools", description="Warnings cleanup sticky messages and forum defaults", guild_only=True)
    selfrole = app_commands.Group(name="selfrole", description="Approved optional roles", guild_only=True)

    def __init__(self, bot, core, features):
        self.bot, self.core, self.features = bot, core, features
        self.state = WishlistStore(core)
        self.db = core.db
        self.role_lock = asyncio.Lock()
        self.thread_lock = asyncio.Lock()
        self.sticky_locks = {}
        self.thread_checked = {}

    async def cog_load(self):
        self.work.start()

    async def cog_unload(self):
        self.work.cancel()

    @jail.command(name="setup", description="Set an ordinary label role for timed jail.")
    @manager
    async def jail_setup(self, interaction: discord.Interaction, role: discord.Role):
        if not self.features.safe_role(role,interaction.guild) or role.id==(self.core.option(interaction.guild_id,"verification") or {}).get("role"):
            raise ValueError("Choose an ordinary role, separate from verification, below Kuro's role.")
        if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
            raise ValueError("Choose a role below your highest role.")
        if self.core.settings(interaction.guild_id)["autorole"] == role.id:
            raise ValueError("The jail role cannot be the automatic join role.")
        self.core.set_option(interaction.guild_id, "jail", {"role": role.id})
        await interaction.response.send_message("Jail label saved. Jail uses Discord timeout to block chatting and voice; the role is a label, not a private-room permission system.", ephemeral=True)

    async def apply_jail(self, guild, actor, member, seconds, reason):
        moderation_target(guild, actor, member)
        if not actor.guild_permissions.moderate_members or not guild.me.guild_permissions.moderate_members:
            raise ValueError("You and Kuro need Moderate Members.")
        if not 1 <= seconds <= 28 * 86400:
            raise ValueError("Jail duration must be at most 28 days.")
        config = self.core.option(guild.id, "jail")
        role = guild.get_role(config["role"]) if config else None
        if not role or not self.features.safe_role(role, guild):
            raise ValueError("Configure an ordinary jail role with /jail setup first.")
        if self.state.jail(guild.id, member.id):
            raise ValueError("That member already has an active Kuro jail. Release it before scheduling another.")
        until = discord.utils.utcnow()+timedelta(seconds=seconds)
        if member.timed_out_until and member.timed_out_until > until:
            raise ValueError("That member already has a longer timeout. Kuro will not shorten it.")
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            raise ValueError("That member already has a timeout managed outside this jail. Leave it in place or clear it explicitly first.")
        await member.timeout(until, reason=f"Kuro jail by {actor.id}: {reason}"[:512])
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jails VALUES (?,?,?,?,?,0,'active',?)", (guild.id, member.id, role.id, int(until.timestamp()), actor.id, reason))
        label = "already present"
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Kuro jail label")
                with self.db:
                    self.db.execute("UPDATE jails SET label_added=1 WHERE guild=? AND member=?", (guild.id, member.id))
                label = "added"
            except discord.HTTPException:
                label = "could not be added; timeout is active"
        return f"Jailed until <t:{int(until.timestamp())}:R>. Label: {label}."

    @jail.command(name="add", description="Timeout and label a member for a duration such as 2h or 3d.")
    @staff
    async def jail_add(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: app_commands.Range[str, 1, 400]):
        seconds = duration_seconds(duration)
        await interaction.response.defer(ephemeral=True)
        actor = await interaction.guild.fetch_member(interaction.user.id)
        target = await interaction.guild.fetch_member(member.id)
        result = await self.apply_jail(interaction.guild, actor, target, seconds, reason)
        await interaction.followup.send(result, ephemeral=True)

    async def release_jail(self, guild, row, manual=False):
        try:
            member = await guild.fetch_member(row["member"])
        except discord.NotFound:
            member = None
        if member:
            current = member.timed_out_until
            if manual and current and abs(current.timestamp()-row["due"]) <= 2:
                await member.timeout(None, reason="Kuro jail released")
            role = guild.get_role(row["role"])
            if row["label_added"] and role and role in member.roles:
                await member.remove_roles(role, reason="Kuro jail ended")
        with self.db:
            self.db.execute("UPDATE jails SET status='released' WHERE guild=? AND member=?", (guild.id, row["member"]))

    @jail.command(name="release", description="Release Kuro's jail without overwriting a separately changed timeout.")
    @staff
    async def jail_release(self, interaction: discord.Interaction, member: discord.Member):
        actor = await interaction.guild.fetch_member(interaction.user.id)
        moderation_target(interaction.guild, actor, member)
        row = self.state.jail(interaction.guild_id, member.id)
        if not row:
            raise ValueError("That member has no active Kuro jail.")
        await interaction.response.defer(ephemeral=True)
        await self.release_jail(interaction.guild, row, manual=True)
        await interaction.followup.send("Kuro's jail released. Any timeout changed separately by a moderator is left intact.", ephemeral=True)

    @jail.command(name="list", description="Show up to ten active jails.")
    @staff
    async def jail_list(self, interaction: discord.Interaction):
        rows = self.db.execute("SELECT * FROM jails WHERE guild=? AND status='active' ORDER BY due LIMIT 10", (interaction.guild_id,)).fetchall()
        await interaction.response.send_message("\n".join(f"{row['member']} until <t:{row['due']}:R> — {row['reason'][:100]}" for row in rows) or "No active jails.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @levels.command(name="setup", description="Enable XP for human chat messages, with a persistent cooldown.")
    @manager
    async def levels_setup(self, interaction: discord.Interaction, enabled: bool, xp_per_message: app_commands.Range[int, 1, 100] = 15, cooldown_seconds: app_commands.Range[int, 15, 3600] = 60):
        if enabled:
            self.features.require_content()
        self.core.set_option(interaction.guild_id, "levels", {"enabled": enabled, "amount": xp_per_message, "cooldown": cooldown_seconds})
        await interaction.response.send_message(f"Activity XP {'enabled' if enabled else 'disabled'}. Level 1 needs 100 XP; level 2 needs 400; level 3 needs 900. Short and repeated messages earn no XP.", ephemeral=True)

    @levels.command(name="rank", description="See your or another member's activity level.")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = self.db.execute("SELECT xp FROM scores WHERE guild=? AND member=?", (interaction.guild_id, member.id)).fetchone()
        xp = row[0] if row else 0
        level = self.state.level(xp)
        await interaction.response.send_message(f"{member.display_name}: level {level}, {xp} XP. Next level: {100*(level+1)**2} XP.", allowed_mentions=NO_MENTIONS)

    @levels.command(name="leaderboard", description="Show this server's top ten activity scores.")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.db.execute("SELECT member,xp FROM scores WHERE guild=? ORDER BY xp DESC,member LIMIT 10", (interaction.guild_id,)).fetchall()
        await interaction.response.send_message("\n".join(f"{index}. <@{row['member']}> — level {self.state.level(row['xp'])}, {row['xp']} XP" for index,row in enumerate(rows, 1)) or "No activity scores yet.", allowed_mentions=NO_MENTIONS)

    @levels.command(name="reward", description="Set an ordinary role reward for reaching a level.")
    @manager
    async def reward(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000], role: discord.Role):
        if not ordinary_role(self.features, interaction.guild, role) or (interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role):
            raise ValueError("Choose an ordinary role below both your and Kuro's roles, separate from verification and jail.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO rewards VALUES (?,?,?)", (interaction.guild_id, level, role.id))
        await interaction.response.send_message("Reward saved. Members receive eligible rewards on their next XP-earning message. Rewards accumulate.", ephemeral=True)

    @levels.command(name="remove_reward", description="Stop automatically granting a level's role reward.")
    @manager
    async def remove_reward(self, interaction: discord.Interaction, level: int):
        with self.db:
            self.db.execute("DELETE FROM rewards WHERE guild=? AND level=?", (interaction.guild_id, level))
        await interaction.response.send_message("Reward removed. Roles already granted remain.", ephemeral=True)

    @watch.command(name="invite", description="Let another server watch membership in this server.")
    @manager
    async def watch_invite(self, interaction: discord.Interaction):
        require_owner(interaction)
        code = self.core.invite("watch", str(interaction.guild_id))
        await interaction.response.send_message(f"Source membership invitation: `{code}`. Give it to the other server's owner. It lasts one hour and can be used once.", ephemeral=True)

    @watch.command(name="add", description="Watch an invited server and flag, jail, or ban overlapping members.")
    @manager
    async def watch_add(self, interaction: discord.Interaction, code: str, action: app_commands.Choice[str], reason: app_commands.Range[str, 1, 300]):
        require_owner(interaction)
        if action.value not in ("flag", "jail", "ban"):
            raise ValueError("Choose flag, jail, or ban.")
        if self.db.execute("SELECT count(*) FROM watches WHERE guild=?", (interaction.guild_id,)).fetchone()[0] >= 10:
            raise ValueError("Remove an existing rule first; at most ten source servers are watched.")
        source = int(self.core.consume_invite("watch", code))
        if source == interaction.guild_id or not self.bot.get_guild(source):
            raise ValueError("Kuro must be in a different source server.")
        if action.value == "jail" and not self.core.option(interaction.guild_id, "jail"):
            raise ValueError("Set up /jail setup before adding an automatic jail rule.")
        with self.db:
            self.db.execute("DELETE FROM watch_flags WHERE guild=? AND source=?",(interaction.guild_id,source))
            self.db.execute("INSERT OR REPLACE INTO watches VALUES (?,?,?,?,?)", (interaction.guild_id, source, action.value, reason, interaction.user.id))
        await interaction.response.send_message("Membership rule saved. New joins are checked. Use /watch scan for existing members. Local shared-ban whitelists also exempt membership-watch actions.", ephemeral=True)

    @watch.command(name="remove", description="Remove a source rule and stop its pending actions.")
    @manager
    async def watch_remove(self, interaction: discord.Interaction, source_server_id: str):
        require_owner(interaction)
        source = int(source_server_id)
        with self.db:
            self.db.execute("DELETE FROM watches WHERE guild=? AND source=?", (interaction.guild_id, source))
            self.db.execute("UPDATE watch_flags SET status='rule removed' WHERE guild=? AND source=? AND status='pending'", (interaction.guild_id, source))
        await interaction.response.send_message("Rule removed. Existing bans and jails remain until explicitly released.", ephemeral=True)

    @watch.command(name="revoke", description="Withdraw this server's permission for another server to watch its membership.")
    @manager
    async def watch_revoke(self, interaction: discord.Interaction, target_server_id: str):
        require_owner(interaction)
        with self.db:
            self.db.execute("DELETE FROM watches WHERE source=? AND guild=?",(interaction.guild_id,int(target_server_id)))
            self.db.execute("UPDATE watch_flags SET status='rule removed' WHERE source=? AND guild=? AND status='pending'",(interaction.guild_id,int(target_server_id)))
        await interaction.response.send_message("Source permission withdrawn for that server. Existing moderation actions are not reversed.",ephemeral=True)

    @watch.command(name="resolve",description="Clear a member's local membership flags without undoing bans or jail.")
    @staff
    async def watch_resolve(self,interaction: discord.Interaction,member: discord.Member):
        with self.db:
            self.db.execute("UPDATE watch_flags SET status='cleared',updated=? WHERE guild=? AND member=?",(int(time.time()),interaction.guild_id,member.id))
        await interaction.response.send_message("Flags cleared. Existing bans and jail remain. Use /whitelist for an ongoing exemption from membership rules.",ephemeral=True)

    @watch.command(name="list", description="Show this server's membership rules and latest flags.")
    @staff
    async def watch_list(self, interaction: discord.Interaction):
        rules = self.db.execute("SELECT * FROM watches WHERE guild=?", (interaction.guild_id,)).fetchall()
        flags = self.db.execute("SELECT * FROM watch_flags WHERE guild=? ORDER BY updated DESC LIMIT 10", (interaction.guild_id,)).fetchall()
        lines = [f"Source {row['source']}: {row['action']}" for row in rules]
        lines += [f"Member {row['member']}: {row['status']} — {row['detail'][:80]}" for row in flags]
        await interaction.response.send_message("\n".join(lines)[:1900] or "No membership rules or flags.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @watch.command(name="scan", description="Scan up to a chosen number of source members and queue matching rules.")
    @staff
    async def watch_scan(self, interaction: discord.Interaction, source_server_id: str, limit: app_commands.Range[int, 1, 10000] = 1000):
        row = self.db.execute("SELECT * FROM watches WHERE guild=? AND source=?", (interaction.guild_id, int(source_server_id))).fetchone()
        source = self.bot.get_guild(int(source_server_id))
        if not row or not source:
            raise ValueError("Configure an invited source that Kuro can access first.")
        await interaction.response.defer(ephemeral=True)
        seen, matches = 0, 0
        async for member in source.fetch_members(limit=limit):
            seen += 1
            target = interaction.guild.get_member(member.id)
            if target and not target.bot and not self.core.whitelisted(interaction.guild_id, target.id):
                self.state.flag(interaction.guild_id, target.id, source.id, row["reason"])
                matches += 1
        await interaction.followup.send(f"Scanned {seen} source members; {matches} overlap matches recorded. Actions are processed in the background. If the limit was reached, this was a partial scan.", ephemeral=True)

    @verification_policy.command(name="setup", description="Route suspicious verification attempts to a moderator review queue.")
    @manager
    async def policy_setup(self, interaction: discord.Interaction, enabled: bool, require_avatar: bool = False, join_burst: app_commands.Range[int, 0, 1000] = 0, failed_challenge_review: app_commands.Range[int,0,20] = 3):
        self.core.set_option(interaction.guild_id, "verification_policy", {"enabled": enabled, "avatar": require_avatar, "burst": join_burst,"failures":failed_challenge_review})
        await interaction.response.send_message("Verification review policy saved. Account age, optional custom avatar, recent join bursts, and membership flags can require review. These signals do not prove alternate accounts.", ephemeral=True)

    async def verification_gate(self, member):
        if self.state.jail(member.guild.id,member.id):
            return "Verification is unavailable during an active jail. Ask a moderator to review it."
        policy = self.core.option(member.guild.id, "verification_policy", {})
        if not policy.get("enabled"):
            return None
        existing = self.db.execute("SELECT status FROM reviews WHERE guild=? AND member=?", (member.guild.id, member.id)).fetchone()
        if existing and existing[0] == "approved":
            return None
        if existing and existing[0] == "rejected":
            return "A moderator has declined verification. Ask the server staff to review that decision."
        reasons = []
        config = self.core.option(member.guild.id, "verification", {})
        if member.created_at > discord.utils.utcnow()-timedelta(days=config.get("min_days", 0)):
            reasons.append("account younger than the configured minimum")
        if policy.get("avatar") and not member.avatar:
            reasons.append("no custom Discord avatar")
        count = self.db.execute("SELECT count(*) FROM recent_joins WHERE guild=? AND joined>?", (member.guild.id, int(time.time())-300)).fetchone()[0]
        if policy.get("burst") and count >= policy["burst"]:
            reasons.append("recent join burst")
        failure_threshold=policy.get("failures",3)
        if failure_threshold and len(self.state.challenge_failures(member.guild.id,member.id))>=failure_threshold:
            reasons.append("repeated incorrect verification challenges")
        if self.db.execute("SELECT 1 FROM verification_links WHERE guild=? AND member=?",(member.guild.id,member.id)).fetchone():
            reasons.append("staff-recorded account association needs review")
        if self.db.execute("SELECT 1 FROM watch_flags WHERE guild=? AND member=? AND status NOT IN ('cleared','rule removed')", (member.guild.id,member.id)).fetchone() and not self.core.whitelisted(member.guild.id, member.id):
            reasons.append("membership rule requires review")
        if reasons:
            self.state.review(member.guild.id, member.id, reasons)
            return "Challenge completed. Your account is waiting for moderator review."
        return None

    @verification_policy.command(name="link",description="Record a manual account association for future verification review.")
    @manager
    async def link_account(self,interaction: discord.Interaction,member: discord.Member,related_user_id: str,note: app_commands.Range[str,1,300]):
        if not related_user_id.isdecimal() or not 0<int(related_user_id)<2**63 or int(related_user_id)==member.id:
            raise ValueError("Enter a different valid Discord user ID.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO verification_links VALUES (?,?,?,?,?)",(interaction.guild_id,member.id,int(related_user_id),note,interaction.user.id))
            self.db.execute("UPDATE reviews SET status='pending' WHERE guild=? AND member=? AND status='approved'",(interaction.guild_id,member.id))
        await interaction.response.send_message("Manual association saved for future verification review when the review policy is enabled. It is a staff note, not proof of shared identity. Existing roles and bans were not changed.",ephemeral=True)

    @verification_policy.command(name="unlink",description="Remove a manual account association from verification checks.")
    @manager
    async def unlink_account(self,interaction: discord.Interaction,member: discord.Member,related_user_id: str):
        if not related_user_id.isdecimal() or not 0<int(related_user_id)<2**63:
            raise ValueError("Enter a valid Discord user ID.")
        with self.db:
            self.db.execute("DELETE FROM verification_links WHERE guild=? AND member=? AND related=?",(interaction.guild_id,member.id,int(related_user_id)))
        await interaction.response.send_message("Manual association removed. Any pending review can still be resolved with /verification_policy decide.",ephemeral=True)

    @verification_policy.command(name="reviews", description="Show the latest ten pending verification reviews.")
    @manager
    async def reviews(self, interaction: discord.Interaction):
        rows = self.db.execute("SELECT * FROM reviews WHERE guild=? AND status='pending' LIMIT 10", (interaction.guild_id,)).fetchall()
        await interaction.response.send_message("\n".join(f"{row['member']}: {', '.join(json.loads(row['reasons']))}" for row in rows)[:1900] or "No pending reviews.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @verification_policy.command(name="decide", description="Approve or reject a queued verification review.")
    @manager
    async def decide(self, interaction: discord.Interaction, member: discord.Member, approve: bool):
        row = self.db.execute("SELECT * FROM reviews WHERE guild=? AND member=?", (interaction.guild_id, member.id)).fetchone()
        if not row:
            raise ValueError("That member has no verification review.")
        await interaction.response.defer(ephemeral=True)
        if approve:
            config = self.core.option(interaction.guild_id, "verification")
            role = interaction.guild.get_role(config["role"]) if config else None
            if not role or not self.features.safe_role(role, interaction.guild):
                raise ValueError("Configure a valid verification role first.")
            if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
                raise ValueError("The verified role is above your authority.")
            if self.state.jail(interaction.guild_id, member.id) or (self.core.global_ban(interaction.guild_id,member.id) and not self.core.whitelisted(interaction.guild_id,member.id)):
                raise ValueError("Resolve the active jail or shared ban before approving verification.")
            await member.add_roles(role, reason=f"Kuro verification approved by {interaction.user.id}")
        with self.db:
            self.db.execute("UPDATE reviews SET status=? WHERE guild=? AND member=?", ("approved" if approve else "rejected", interaction.guild_id,member.id))
        await interaction.followup.send("Verification approved and role granted." if approve else "Verification declined. No ban was issued.", ephemeral=True)

    def role_snapshot(self, role):
        return {"name": role.name, "permissions": role.permissions.value, "colour": role.colour.value, "hoist": role.hoist, "mentionable": role.mentionable, "position": role.position, "members": [member.id for member in role.members]}

    def thread_snapshot(self, thread):
        return {"name": thread.name, "parent": thread.parent_id, "type": thread.type.value, "archive": thread.auto_archive_duration, "slowmode": thread.slowmode_delay, "archived": thread.archived, "locked": thread.locked, "private": thread.is_private(), "members": [member.id for member in thread.members]}

    @recovery_plus.command(name="setup", description="Snapshot deleted-role and thread recovery plus archive limits.")
    @manager
    async def recovery_setup(self, interaction: discord.Interaction, roles: bool, threads: bool, messages_per_channel: app_commands.Range[int, 100, 10000] = 1000, replay_count: app_commands.Range[int, 0, 100] = 50, include_bots: bool = False):
        require_owner(interaction)
        if not self.core.option(interaction.guild_id, "recovery", {}).get("enabled"):
            raise ValueError("Enable /recovery_setup first, then configure these extra options.")
        await interaction.response.defer(ephemeral=True)
        previous=self.core.option(interaction.guild_id,"recovery_plus",{})
        with self.db:
            if roles and not previous.get("roles"):
                self.db.execute("DELETE FROM entity_snapshots WHERE guild=? AND kind='role'",(interaction.guild_id,))
                self.db.execute("DELETE FROM recovered_role_members WHERE guild=? AND status='pending'",(interaction.guild_id,))
            if threads and not previous.get("threads"):
                self.db.execute("DELETE FROM entity_snapshots WHERE guild=? AND kind='thread'",(interaction.guild_id,))
        self.core.set_option(interaction.guild_id, "recovery_plus", {"roles": roles, "threads": threads, "limit": messages_per_channel, "replay": replay_count, "bots": include_bots})
        if roles:
            for role in interaction.guild.roles:
                if not role.managed and not role.is_default():
                    self.state.entity(interaction.guild_id, "role", role.id, self.role_snapshot(role))
        if threads:
            for thread in interaction.guild.threads:
                self.state.entity(interaction.guild_id, "thread", thread.id, self.thread_snapshot(thread))
        await interaction.followup.send("Extra recovery saved. Active visible threads were snapshotted; archived threads are captured with /recovery_plus history. Staff roles may be recreated, but staff assignments require manual owner review.", ephemeral=True)

    @recovery_plus.command(name="history", description="Capture existing history and accessible archived threads from a channel.")
    @manager
    async def capture_history(self, interaction: discord.Interaction, channel: discord.TextChannel | discord.ForumChannel, limit: app_commands.Range[int, 1, 1000] = 100, include_threads: bool = False):
        self.features.require_content()
        self.features.check_channel(interaction, channel)
        if not channel.permissions_for(interaction.user).read_message_history:
            raise ValueError("You need Read Message History in that channel.")
        if not self.core.option(interaction.guild_id, "recovery", {}).get("archive"):
            raise ValueError("Enable message backups in /recovery_setup first.")
        await interaction.response.defer(ephemeral=True)
        sources = [channel] if isinstance(channel,discord.TextChannel) else []
        if include_threads:
            sources += list(channel.threads)
            async for thread in channel.archived_threads(limit=100):
                if thread.id not in {item.id for item in sources}:
                    sources.append(thread)
        captured = 0
        for source in sources:
            if isinstance(source,discord.Thread):
                if source.is_private():
                    continue
                self.state.entity(interaction.guild_id,"thread",source.id,self.thread_snapshot(source))
            count = 0
            async for message in source.history(limit=limit):
                if self.should_archive(message):
                    await self.features.archive_message(message)
                    count += 1
            captured += count
        await interaction.followup.send(f"Captured {captured} messages. Limit: {limit} per channel/thread, up to 100 archived threads; retention limits still apply. Private threads are not copied.", ephemeral=True)

    def should_archive(self, message):
        config = self.core.option(message.guild.id,"recovery_plus",{})
        return message.author.id != self.bot.user.id and (not message.author.bot or config.get("bots",False))

    @recovery_plus.command(name="role_review",description="Export former members of a recovered sensitive role for owner review.")
    @manager
    async def role_review(self,interaction: discord.Interaction,role: discord.Role):
        require_owner(interaction)
        row=self.db.execute("SELECT former_members FROM recovered_staff_roles WHERE guild=? AND role=?",(interaction.guild_id,role.id)).fetchone()
        if not row:
            raise ValueError("No sensitive assignment review record exists for that role.")
        data=json.dumps({"role":role.id,"name":role.name,"former_member_ids":json.loads(row[0])},indent=2).encode()
        if len(data)>interaction.guild.filesize_limit:
            raise ValueError("Review file exceeds this server's upload limit. Retrieve the database from the persistent volume.")
        await interaction.response.send_message("Review these former assignments before manually granting this sensitive role. Kuro has not restored them automatically.",file=discord.File(io.BytesIO(data),filename=f"role-review-{role.id}.json"),ephemeral=True)

    async def restore_roles(self, guild):
        if not self.core.option(guild.id,"recovery_plus",{}).get("roles"):
            return
        async with self.role_lock:
            for old, snapshot in sorted(self.state.entities(guild.id,"role"), key=lambda item:item[1]["position"]):
                if guild.get_role(old):
                    continue
                if snapshot["permissions"] & ~guild.me.guild_permissions.value:
                    log.warning("Role %s recovery requires permissions Kuro does not have",old)
                    continue
                try:
                    created = await guild.create_role(name=snapshot["name"],permissions=discord.Permissions(snapshot["permissions"]),colour=discord.Colour(snapshot["colour"]),hoist=snapshot["hoist"],mentionable=snapshot["mentionable"],reason="Kuro deleted role recovery")
                    self.state.remap_role(guild.id,old,created.id)
                    try:
                        await created.edit(position=min(snapshot["position"], max(1,guild.me.top_role.position-1)), reason="Kuro role order recovery")
                    except discord.HTTPException:
                        log.warning("Recovered role %s position needs owner review",created.id)
                    if ordinary_role(self.features,guild,created):
                        with self.db:
                            self.db.executemany("INSERT OR IGNORE INTO recovered_role_members(guild,role,member) VALUES (?,?,?)",[(guild.id,created.id,user_id) for user_id in snapshot["members"]])
                    else:
                        with self.db:
                            self.db.execute("INSERT OR REPLACE INTO recovered_staff_roles VALUES (?,?,?)",(guild.id,created.id,json.dumps(snapshot["members"])))
                        log.info("Recovered staff/verification/jail role %s; assignments need owner review",created.id)
                except discord.HTTPException as exc:
                    log.warning("Role %s recovery failed: HTTP %s",old,exc.status)
                break

    async def restore_threads(self, guild):
        if not self.core.option(guild.id,"recovery_plus",{}).get("threads"):
            return
        async with self.thread_lock:
            checked=0
            for old,snapshot in self.state.entities(guild.id,"thread"):
                if snapshot.get("private"):
                    continue
                if self.thread_checked.get(old,0)>time.monotonic():
                    continue
                checked+=1
                if checked>3:
                    break
                try:
                    await guild.fetch_channel(old)
                    self.thread_checked[old]=time.monotonic()+300
                    continue
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    self.thread_checked[old]=time.monotonic()+60
                    continue
                parent = guild.get_channel(snapshot["parent"])
                if not isinstance(parent,(discord.TextChannel,discord.ForumChannel)):
                    continue
                try:
                    if isinstance(parent,discord.ForumChannel):
                        result = await parent.create_thread(name=snapshot["name"],content="Recovered forum post. Saved replies follow below.",auto_archive_duration=snapshot["archive"],slowmode_delay=snapshot["slowmode"],reason="Kuro thread recovery",allowed_mentions=NO_MENTIONS)
                        thread = result.thread
                    else:
                        thread = await parent.create_thread(name=snapshot["name"],type=discord.ChannelType.public_thread,auto_archive_duration=snapshot["archive"],slowmode_delay=snapshot["slowmode"],reason="Kuro thread recovery")
                    with self.db:
                        self.db.execute("UPDATE entity_snapshots SET entity=? WHERE guild=? AND kind='thread' AND entity=?",(thread.id,guild.id,old))
                        self.db.execute("UPDATE message_backups SET channel=? WHERE guild=? AND channel=?",(thread.id,guild.id,old))
                    await self.features.replay_messages(thread)
                    if snapshot["archived"] or snapshot["locked"]:
                        await thread.edit(archived=snapshot["archived"],locked=snapshot["locked"],reason="Kuro recovered thread state")
                except discord.HTTPException as exc:
                    log.warning("Thread %s recovery failed: HTTP %s",old,exc.status)
                    self.thread_checked[old]=time.monotonic()+60
                break

    @blueprint.command(name="create", description="Create a one-hour invitation to copy this server's structure.")
    @manager
    async def blueprint_create(self, interaction: discord.Interaction):
        require_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        payload = {"source":interaction.guild_id,"name":interaction.guild.name,"roles":[],"channels":[],"bot_roles":{}}
        for role in interaction.guild.roles:
            if role.tags and role.tags.bot_id:
                payload["bot_roles"][str(role.id)]=role.tags.bot_id
            if not role.managed and not role.is_default():
                data = self.role_snapshot(role)
                data.pop("members")
                payload["roles"].append([role.id,data])
        for channel in await interaction.guild.fetch_channels():
            if (snapshot:=self.features.channel_snapshot(channel)):
                snapshot["default_view"] = channel.permissions_for(interaction.guild.default_role).view_channel
                payload["channels"].append([channel.id,snapshot])
        payload["channels"].sort(key=lambda item:(item[1]["type"]!=4,item[1]["position"]))
        key = secrets.token_hex(16)
        with self.db:
            self.db.execute("INSERT INTO blueprints VALUES (?,?)",(key,json.dumps(payload)))
        code = self.core.invite("blueprint",key)
        await interaction.followup.send(f"Blueprint invitation: `{code}`. The destination server's owner runs /blueprint import and confirms the summary. This copies structure, not users, messages, bots, or credentials.",ephemeral=True)

    @blueprint.command(name="import", description="Preview and confirm an invited structure copy into this server.")
    @manager
    async def blueprint_import(self, interaction: discord.Interaction, code: str):
        require_owner(interaction)
        import hashlib
        row = self.db.execute("SELECT b.payload FROM invitations i JOIN blueprints b ON b.id=i.target WHERE i.hash=? AND i.kind='blueprint' AND i.expires>?",(hashlib.sha256(code.encode()).hexdigest(),int(time.time()))).fetchone()
        if not row:
            raise ValueError("That blueprint invitation is invalid or expired.")
        if self.db.execute("SELECT 1 FROM blueprint_jobs WHERE guild=? AND status IN ('queued','working')",(interaction.guild_id,)).fetchone():
            raise ValueError("A blueprint is already being imported here.")
        payload = json.loads(row[0])
        await interaction.response.send_message(f"Copy **{discord.utils.escape_markdown(payload['name'])}** into this server? This adds {len(payload['roles'])} role definitions and {len(payload['channels'])} channels. Existing channels stay. Kuro cannot copy unavailable permissions; imports stop for review if a step fails.",view=BlueprintConfirm(self,code,interaction.user.id),ephemeral=True,allowed_mentions=NO_MENTIONS)

    @blueprint.command(name="status", description="Show the most recent structure copy job.")
    @manager
    async def blueprint_status(self, interaction: discord.Interaction):
        row = self.db.execute("SELECT * FROM blueprint_jobs WHERE guild=? ORDER BY id DESC LIMIT 1",(interaction.guild_id,)).fetchone()
        if not row:
            raise ValueError("No blueprint jobs in this server.")
        data = json.loads(row["payload"])
        await interaction.response.send_message(f"Job #{row['id']}: {row['status']}. Roles {data['role_step']}/{len(data['roles'])}; channels {data['channel_step']}/{len(data['channels'])}. {row['error'] or ''}",ephemeral=True,allowed_mentions=NO_MENTIONS)

    @blueprint.command(name="cancel", description="Cancel pending copy steps. Already created structure remains.")
    @manager
    async def blueprint_cancel(self, interaction: discord.Interaction):
        require_owner(interaction)
        with self.db:
            self.db.execute("UPDATE blueprint_jobs SET status='cancelled' WHERE guild=? AND status IN ('queued','working')",(interaction.guild_id,))
        await interaction.response.send_message("Pending import steps cancelled. Existing and already-created channels/roles remain.",ephemeral=True)

    @protection.command(name="setup", description="Send owner-change and bot-removal alerts to a chosen channel.")
    @manager
    async def protection_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool=True):
        require_owner(interaction)
        self.features.check_channel(interaction,channel)
        self.core.set_option(interaction.guild_id,"protection",{"enabled":enabled,"channel":channel.id,"owner":interaction.user.id})
        await interaction.response.send_message("Owner-change and removed-bot alerts configured. Use /protection lock for manual reversible lockdown. A removed bot cannot continue acting in that server.",ephemeral=True)

    LOCK_FIELDS = ("send_messages","send_messages_in_threads","create_public_threads","create_private_threads","add_reactions","connect")

    async def lock_channel(self,channel):
        targets = dict(channel.overwrites)
        targets.setdefault(channel.guild.default_role,discord.PermissionOverwrite())
        targets.setdefault(channel.guild.me,channel.overwrites_for(channel.guild.me))
        failures = 0
        for target,overwrite in targets.items():
            if self.db.execute("SELECT 1 FROM lock_states WHERE guild=? AND channel=? AND target=?",(channel.guild.id,channel.id,target.id)).fetchone():
                continue
            previous = {key:getattr(overwrite,key) for key in self.LOCK_FIELDS}
            applied = {key:target.id==channel.guild.me.id for key in self.LOCK_FIELDS}
            if target.id != channel.guild.me.id and target != channel.guild.default_role:
                applied = {key:False for key in self.LOCK_FIELDS if getattr(overwrite,key) is True}
            if not applied:
                continue
            with self.db:
                self.db.execute("INSERT INTO lock_states VALUES (?,?,?,?,?,?)",(channel.guild.id,channel.id,target.id,"role" if isinstance(target,discord.Role) or (isinstance(target,discord.Object) and target.type is discord.Role) else "member",json.dumps(previous),json.dumps(applied)))
            for key,value in applied.items():
                setattr(overwrite,key,value)
            try:
                await channel.set_permissions(target,overwrite=overwrite,reason="Kuro owner-requested lockdown")
            except discord.HTTPException:
                failures += 1
        return failures

    @protection.command(name="lock", description="Block ordinary chat and voice with saved permission changes.")
    @manager
    async def lockdown(self,interaction: discord.Interaction):
        require_owner(interaction)
        if not interaction.guild.me.guild_permissions.manage_roles:
            raise ValueError("Kuro needs Manage Roles to change channel permission overwrites.")
        await interaction.response.defer(ephemeral=True)
        self.core.set_option(interaction.guild_id,"lockdown",True)
        failures = 0
        for channel in interaction.guild.channels:
            failures += await self.lock_channel(channel)
        await interaction.followup.send(f"Lockdown processed. Failed permission changes: {failures}. Administrators can still act. /protection unlock restores Kuro's changes.",ephemeral=True)

    @protection.command(name="unlock", description="Restore saved lockdown permissions while preserving unrelated changes.")
    @manager
    async def unlock(self,interaction: discord.Interaction):
        require_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        self.core.set_option(interaction.guild_id,"lockdown",False)
        failures = 0
        for row in self.db.execute("SELECT * FROM lock_states WHERE guild=?",(interaction.guild_id,)).fetchall():
            channel = interaction.guild.get_channel(row["channel"])
            target = interaction.guild.get_role(row["target"]) if row["kind"]=="role" else discord.Object(id=row["target"])
            if channel and target:
                overwrite = channel.overwrites_for(target)
                original,applied = json.loads(row["original"]),json.loads(row["applied"])
                for key,expected in applied.items():
                    if getattr(overwrite,key) is expected:
                        setattr(overwrite,key,original[key])
                try:
                    await channel.set_permissions(target,overwrite=None if overwrite.is_empty() else overwrite,reason="Kuro lockdown ended")
                except discord.HTTPException:
                    failures += 1
                    continue
            with self.db:
                self.db.execute("DELETE FROM lock_states WHERE guild=? AND channel=? AND target=?",(row["guild"],row["channel"],row["target"]))
        await interaction.followup.send(f"Unlock processed. Remaining failures: {failures}. Run again after fixing permissions if needed.",ephemeral=True)

    @tools.command(name="warn", description="Record a moderator warning without sending a DM.")
    @staff
    async def warn(self,interaction: discord.Interaction,member: discord.Member,reason: app_commands.Range[str,1,400]):
        moderation_target(interaction.guild,interaction.user,member)
        with self.db:
            cursor=self.db.execute("INSERT INTO warnings(guild,member,actor,reason,created) VALUES (?,?,?,?,?)",(interaction.guild_id,member.id,interaction.user.id,reason,int(time.time())))
        await interaction.response.send_message(f"Warning #{cursor.lastrowid} recorded.",ephemeral=True)

    @tools.command(name="warnings", description="Show a member's most recent ten warnings.")
    @staff
    async def warnings(self,interaction: discord.Interaction,member: discord.Member):
        rows=self.db.execute("SELECT * FROM warnings WHERE guild=? AND member=? ORDER BY id DESC LIMIT 10",(interaction.guild_id,member.id)).fetchall()
        await interaction.response.send_message("\n".join(f"#{row['id']}: {row['reason']}" for row in rows)[:1900] or "No warnings.",ephemeral=True,allowed_mentions=NO_MENTIONS)

    @tools.command(name="purge", description="Delete up to 100 recent messages in this channel.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge(self,interaction: discord.Interaction,count: app_commands.Range[int,1,100]):
        if not isinstance(interaction.channel,(discord.TextChannel,discord.Thread)):
            raise ValueError("Use purge in a text channel or thread.")
        await interaction.response.defer(ephemeral=True)
        deleted=await interaction.channel.purge(limit=count,reason=f"Kuro purge by {interaction.user.id}")
        await interaction.followup.send(f"Deleted {len(deleted)} messages. Existing backups are retained.",ephemeral=True)

    @tools.command(name="sticky", description="Set or disable a reminder that returns after channel activity.")
    @manager
    async def sticky(self,interaction: discord.Interaction,channel: discord.TextChannel,enabled: bool,text: app_commands.Range[str,1,1800]="Please read the channel rules."):
        self.features.check_channel(interaction,channel)
        with self.db:
            if enabled:
                self.db.execute("INSERT OR REPLACE INTO stickies VALUES (?,?,?,NULL,0)",(interaction.guild_id,channel.id,text))
            else:
                self.db.execute("DELETE FROM stickies WHERE channel=? AND guild=?",(channel.id,interaction.guild_id))
        await interaction.response.send_message("Sticky reminder saved. It reposts after human activity, at most once per minute." if enabled else "Sticky reminder disabled. The last posted reminder remains.",ephemeral=True)

    @tools.command(name="forum", description="Set forum guidelines and default archive duration.")
    @manager
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def forum(self,interaction: discord.Interaction,channel: discord.ForumChannel,guidelines: app_commands.Range[str,1,2000],archive_minutes: app_commands.Choice[int]):
        self.features.check_channel(interaction,channel)
        if archive_minutes.value not in (60,1440,4320,10080):
            raise ValueError("Choose a supported archive duration.")
        await interaction.response.defer(ephemeral=True)
        await channel.edit(topic=guidelines,default_auto_archive_duration=archive_minutes.value,reason=f"Kuro forum defaults by {interaction.user.id}")
        await interaction.followup.send("Forum guidelines and default archive duration saved. Archiving hides inactive posts; it does not delete them.",ephemeral=True)

    @selfrole.command(name="setup",description="Allow or disallow an ordinary role in the member role picker.")
    @manager
    async def selfrole_setup(self,interaction: discord.Interaction,role: discord.Role,enabled: bool):
        if enabled and (not ordinary_role(self.features,interaction.guild,role) or (interaction.user.id!=interaction.guild.owner_id and role>=interaction.user.top_role)):
            raise ValueError("Choose an ordinary optional role below your and Kuro's roles, separate from verification and jail.")
        with self.db:
            if enabled:
                self.db.execute("INSERT OR IGNORE INTO selfroles VALUES (?,?)",(interaction.guild_id,role.id))
            else:
                self.db.execute("DELETE FROM selfroles WHERE guild=? AND role=?",(interaction.guild_id,role.id))
        await interaction.response.send_message("Optional role list updated.",ephemeral=True)

    @selfrole.command(name="pick",description="Add or remove an approved optional role for yourself.")
    async def selfrole_pick(self,interaction: discord.Interaction,role: discord.Role,enabled: bool):
        if not self.db.execute("SELECT 1 FROM selfroles WHERE guild=? AND role=?",(interaction.guild_id,role.id)).fetchone() or not ordinary_role(self.features,interaction.guild,role):
            raise ValueError("That role is not an approved ordinary optional role.")
        if self.state.jail(interaction.guild_id,interaction.user.id):
            raise ValueError("Optional role changes are unavailable during jail.")
        await interaction.response.defer(ephemeral=True)
        method=interaction.user.add_roles if enabled else interaction.user.remove_roles
        await method(role,reason="Kuro optional role picker")
        await interaction.followup.send("Optional role updated.",ephemeral=True)

    @selfrole.command(name="list",description="Show roles you can choose for yourself.")
    async def selfrole_list(self,interaction: discord.Interaction):
        roles=[interaction.guild.get_role(row[0]) for row in self.db.execute("SELECT role FROM selfroles WHERE guild=?",(interaction.guild_id,))]
        await interaction.response.send_message(", ".join(role.mention for role in roles if role and ordinary_role(self.features,interaction.guild,role)) or "No optional roles configured.",ephemeral=True,allowed_mentions=NO_MENTIONS)

    @commands.Cog.listener()
    async def on_message(self,message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        config=self.core.option(message.guild.id,"levels",{})
        if config.get("enabled") and self.bot.intents.message_content and not self.state.jail(message.guild.id,message.author.id):
            xp=self.state.award(message.guild.id,message.author.id,message.content,config["amount"],config["cooldown"])
            if xp is not None:
                for row in self.db.execute("SELECT role FROM rewards WHERE guild=? AND level<=?",(message.guild.id,self.state.level(xp))).fetchall():
                    role=message.guild.get_role(row[0])
                    if role and role not in message.author.roles and ordinary_role(self.features,message.guild,role):
                        try:
                            await message.author.add_roles(role,reason="Kuro activity reward")
                        except discord.HTTPException:
                            log.warning("Activity reward role %s could not be granted",role.id)
        if isinstance(message.channel,discord.TextChannel):
            lock=self.sticky_locks.setdefault(message.channel.id,asyncio.Lock())
            async with lock:
                row=self.db.execute("SELECT * FROM stickies WHERE channel=?",(message.channel.id,)).fetchone()
                if row and int(time.time())-row["sent"]>=60:
                    try:
                        posted=await message.channel.send(row["content"],allowed_mentions=NO_MENTIONS)
                        if row["message"]:
                            try:
                                await message.channel.get_partial_message(row["message"]).delete()
                            except discord.HTTPException:
                                pass
                        with self.db:
                            self.db.execute("UPDATE stickies SET message=?,sent=? WHERE channel=?",(posted.id,int(time.time()),message.channel.id))
                    except discord.HTTPException:
                        log.warning("Sticky reminder delivery failed in %s",message.channel.id)

    @commands.Cog.listener()
    async def on_member_join(self,member):
        if member.bot:
            return
        with self.db:
            self.db.execute("DELETE FROM recent_joins WHERE joined<?",(int(time.time())-3600,))
            self.db.execute("INSERT OR REPLACE INTO recent_joins VALUES (?,?,?)",(member.guild.id,member.id,int(time.time())))
        jail=self.state.jail(member.guild.id,member.id)
        if jail and jail["due"]>time.time() and not member.guild_permissions.administrator and member.top_role<member.guild.me.top_role:
            try:
                await member.timeout(discord.utils.utcnow()+timedelta(seconds=jail["due"]-time.time()),reason="Kuro jail restored after rejoining")
                role=member.guild.get_role(jail["role"])
                if role and self.features.safe_role(role,member.guild):
                    await member.add_roles(role,reason="Kuro jail label restored")
                    with self.db:
                        self.db.execute("UPDATE jails SET label_added=1 WHERE guild=? AND member=?",(member.guild.id,member.id))
            except discord.HTTPException:
                log.warning("Could not reapply jail in %s",member.guild.id)
        for row in self.db.execute("SELECT * FROM watches WHERE guild=? OR source=?",(member.guild.id,member.guild.id)).fetchall():
            if row["guild"]==member.guild.id:
                source=self.bot.get_guild(row["source"])
                if source:
                    try:
                        await source.fetch_member(member.id)
                        self.state.flag(member.guild.id,member.id,row["source"],row["reason"])
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        log.warning("Membership check unavailable for source %s",source.id)
            else:
                target=self.bot.get_guild(row["guild"])
                if target and target.get_member(member.id):
                    self.state.flag(target.id,member.id,member.guild.id,row["reason"])

    @commands.Cog.listener()
    async def on_member_update(self,before,after):
        if before.roles!=after.roles and self.core.option(after.guild.id,"recovery_plus",{}).get("roles"):
            for role in set(before.roles+after.roles):
                if not role.managed and not role.is_default() and after.guild.get_role(role.id):
                    self.state.entity(after.guild.id,"role",role.id,self.role_snapshot(role))

    @commands.Cog.listener()
    async def on_guild_role_create(self,role):
        if self.core.option(role.guild.id,"recovery_plus",{}).get("roles") and not role.managed and not role.is_default():
            async with self.role_lock:
                self.state.entity(role.guild.id,"role",role.id,self.role_snapshot(role))

    @commands.Cog.listener()
    async def on_guild_role_update(self,before,after):
        await self.on_guild_role_create(after)

    @commands.Cog.listener()
    async def on_guild_role_delete(self,role):
        # Saved role definitions are the durable queue; the healing tick restores them.
        pass

    @commands.Cog.listener()
    async def on_thread_create(self,thread):
        if self.core.option(thread.guild.id,"recovery_plus",{}).get("threads") and not thread.is_private():
            async with self.thread_lock:
                self.state.entity(thread.guild.id,"thread",thread.id,self.thread_snapshot(thread))

    @commands.Cog.listener()
    async def on_thread_update(self,before,after):
        await self.on_thread_create(after)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self,payload):
        self.thread_checked.pop(payload.thread_id,None)

    @commands.Cog.listener()
    async def on_guild_channel_create(self,channel):
        if self.core.option(channel.guild.id,"lockdown",False):
            await self.lock_channel(channel)

    @commands.Cog.listener()
    async def on_guild_update(self,before,after):
        if before.owner_id==after.owner_id:
            return
        config=self.core.option(after.id,"protection",{})
        channel=after.get_channel(config.get("channel"))
        if config.get("enabled") and channel:
            try:
                await channel.send(f"**Owner changed:** {before.owner_id} → {after.owner_id}. No destructive action was taken. The current owner can inspect backups and use /protection lock if needed.",allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Owner-change alert failed in %s",after.id)

    @commands.Cog.listener()
    async def on_member_remove(self,member):
        if not member.bot or member.id==self.bot.user.id:
            return
        config=self.core.option(member.guild.id,"protection",{})
        channel=member.guild.get_channel(config.get("channel"))
        if config.get("enabled") and channel:
            try:
                await channel.send(f"A bot left or was removed: {member.name} ({member.id}). Review the server audit log if this was unexpected.",allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Removed-bot alert failed in %s",member.guild.id)

    async def process_watch(self,row):
        rule=self.db.execute("SELECT * FROM watches WHERE guild=? AND source=?",(row["guild"],row["source"])).fetchone()
        guild=self.bot.get_guild(row["guild"])
        source=self.bot.get_guild(row["source"])
        result="flagged"
        try:
            if not rule or not guild or not source:
                result="unavailable or removed rule"
            elif self.core.whitelisted(guild.id,row["member"]):
                result="whitelisted"
            else:
                # A stale scan is never enough to execute an automatic ban.
                await source.fetch_member(row["member"])
                member=await guild.fetch_member(row["member"])
                actor=await guild.fetch_member(rule["actor"])
                fresh_rule=self.db.execute("SELECT * FROM watches WHERE guild=? AND source=?",(row["guild"],row["source"])).fetchone()
                if not fresh_rule or tuple(fresh_rule)!=tuple(rule):
                    result="rule changed or revoked"
                elif self.core.whitelisted(guild.id,row["member"]):
                    result="whitelisted"
                elif actor.id!=guild.owner_id:
                    result="owner changed; rule needs reapproval"
                elif rule["action"]=="ban":
                    from features import check_ban
                    check_ban(guild,actor,member,self.bot.user.id)
                    await guild.ban(member,reason=f"Kuro membership rule: {rule['reason']}"[:512],delete_message_seconds=0)
                    result="banned"
                elif rule["action"]=="jail":
                    await self.apply_jail(guild,actor,member,86400,rule["reason"])
                    result="jailed for 1 day"
        except discord.NotFound:
            result="member no longer present"
        except (discord.HTTPException,ValueError) as exc:
            result="failed: "+str(exc)[:200]
        with self.db:
            self.db.execute("UPDATE watch_flags SET status=?,updated=? WHERE guild=? AND member=? AND source=?",(result,int(time.time()),row["guild"],row["member"],row["source"]))

    async def blueprint_step(self,row):
        guild=self.bot.get_guild(row["guild"])
        if not guild or guild.owner_id!=row["actor"]:
            with self.db:
                self.db.execute("UPDATE blueprint_jobs SET status='failed',error='Current owner must match the confirming owner' WHERE id=?",(row["id"],))
            return
        data=json.loads(row["payload"])
        with self.db:
            self.db.execute("UPDATE blueprint_jobs SET status='working' WHERE id=?",(row["id"],))
        try:
            if data["role_step"]<len(data["roles"]):
                old,snapshot=data["roles"][data["role_step"]]
                if snapshot["permissions"] & ~guild.me.guild_permissions.value:
                    raise ValueError("Kuro lacks a permission required by a source role. No more steps will run; inspect the partial copy before creating a new job.")
                role=await guild.create_role(name=snapshot["name"],permissions=discord.Permissions(snapshot["permissions"]),colour=discord.Colour(snapshot["colour"]),hoist=snapshot["hoist"],mentionable=snapshot["mentionable"],reason="Kuro owner-approved blueprint")
                data["role_map"][str(old)]=role.id
                data["role_step"]+=1
            elif data["channel_step"]<len(data["channels"]):
                old,snapshot=data["channels"][data["channel_step"]]
                snapshot=json.loads(json.dumps(snapshot))
                for overwrite in snapshot["overwrites"]:
                    if overwrite["role"]:
                        if str(overwrite["id"]) not in data["role_map"]:
                            raise ValueError("A permission role cannot be mapped. Channel not created to protect access rules.")
                        overwrite["id"]=data["role_map"][str(overwrite["id"])]
                    else:
                        raise ValueError("This channel has individual-member permission overwrites. Resolve those manually before copying its blueprint.")
                if snapshot.get("category"):
                    snapshot["category"]=data["channel_map"].get(str(snapshot["category"]))
                    if not snapshot["category"]:
                        raise ValueError("Parent category was not copied.")
                if not snapshot.get("default_view",True):
                    everyone=next((item for item in snapshot["overwrites"] if item["id"]==guild.id),None)
                    if everyone is None:
                        everyone={"id":guild.id,"role":True,"allow":0,"deny":0}
                        snapshot["overwrites"].append(everyone)
                    bit=discord.Permissions.view_channel.flag
                    everyone["allow"] &= ~bit
                    everyone["deny"] |= bit
                created=await self.features.restore_channel(guild,old,snapshot)
                data["channel_map"][str(old)]=created.id
                data["channel_step"]+=1
            status="completed" if data["role_step"]==len(data["roles"]) and data["channel_step"]==len(data["channels"]) else "queued"
            with self.db:
                self.db.execute("UPDATE blueprint_jobs SET payload=?,status=? WHERE id=? AND status='working'",(json.dumps(data),status,row["id"]))
        except (ValueError,discord.HTTPException) as exc:
            with self.db:
                self.db.execute("UPDATE blueprint_jobs SET payload=?,status='failed',error=? WHERE id=?",(json.dumps(data),str(exc)[:500],row["id"]))

    @tasks.loop(seconds=15)
    async def work(self):
        try:
            for row in self.db.execute("SELECT * FROM recovered_role_members WHERE status='pending' LIMIT 5").fetchall():
                if not self.core.option(row["guild"],"recovery",{}).get("enabled") or not self.core.option(row["guild"],"recovery_plus",{}).get("roles"):
                    continue
                guild=self.bot.get_guild(row["guild"])
                role=guild.get_role(row["role"]) if guild else None
                member=guild.get_member(row["member"]) if guild else None
                if role and member and ordinary_role(self.features,guild,role) and not self.state.jail(guild.id,member.id) and not (self.core.global_ban(guild.id,member.id) and not self.core.whitelisted(guild.id,member.id)):
                    try:
                        await member.add_roles(role,reason="Kuro recovered ordinary role assignment")
                        status="restored"
                    except discord.HTTPException:
                        status="needs review"
                else:
                    status="skipped"
                with self.db:
                    self.db.execute("UPDATE recovered_role_members SET status=? WHERE guild=? AND role=? AND member=?",(status,row["guild"],row["role"],row["member"]))
            for row in self.db.execute("SELECT * FROM jails WHERE status='active' AND due<=?",(int(time.time()),)).fetchall():
                guild=self.bot.get_guild(row["guild"])
                if guild:
                    try:
                        await self.release_jail(guild,row)
                    except discord.HTTPException:
                        log.warning("Jail label cleanup failed for member %s",row["member"])
            for row in self.db.execute("SELECT * FROM watch_flags WHERE status='pending' ORDER BY updated LIMIT 5").fetchall():
                await self.process_watch(row)
            row=self.db.execute("SELECT * FROM blueprint_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                await self.blueprint_step(row)
        except Exception as exc:
            log.error("Background wishlist work failed: %s",type(exc).__name__)

    @app_commands.command(description="Open Kuro's beginner guide with setup steps and examples.")
    @app_commands.guild_only()
    async def guide(self,interaction: discord.Interaction):
        await interaction.response.send_message("[Open the Kuro beginner guide](https://github.com/Rogue-cpu/Kurobot/blob/main/docs/BEGINNER_GUIDE.md)\nStart with roles and verification, then enable the features you want. Use /features_status and /recovery_status to check your setup.",ephemeral=True)

    @work.before_loop
    async def before_work(self):
        await self.bot.wait_until_ready()
        with self.db:
            self.db.execute("UPDATE blueprint_jobs SET status='needs review',error='Interrupted during a creation step; inspect existing structure before making another import' WHERE status='working'")


# Stable choices are attached after Cog construction so Discord renders dropdowns.
Wishlist.watch_add = app_commands.choices(action=[app_commands.Choice(name=value,value=value) for value in ("flag","jail","ban")])(Wishlist.watch_add)
Wishlist.forum = app_commands.choices(archive_minutes=[app_commands.Choice(name=name,value=value) for name,value in (("1 hour",60),("1 day",1440),("3 days",4320),("7 days",10080))])(Wishlist.forum)
