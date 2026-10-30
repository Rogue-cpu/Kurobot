"""Opt-in server features for Kuro. No credentials or deployment setup lives here."""
import asyncio
import io
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from difflib import get_close_matches

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import duration_seconds

log = logging.getLogger("kuro.features")
NO_MENTIONS = discord.AllowedMentions.none()


def manager(callback):
    callback = app_commands.guild_only()(callback)
    callback = app_commands.default_permissions(manage_guild=True)(callback)
    return app_commands.checks.has_permissions(manage_guild=True)(callback)


def moderator(callback):
    callback = app_commands.guild_only()(callback)
    callback = app_commands.default_permissions(ban_members=True)(callback)
    return app_commands.checks.has_permissions(ban_members=True)(callback)


def require_owner(interaction):
    if interaction.user.id != interaction.guild.owner_id:
        raise ValueError("Only this server's owner can manage ban-network membership.")


def parse_deathnote(content):
    parts = [part.strip() for part in content.split("|", 2)]
    if len(parts) != 3 or not parts[0] or not 1 <= len(parts[2]) <= 400:
        raise ValueError("Use: name or @member | 10m | reason (up to 400 characters).")
    return parts[0], duration_seconds(parts[1]), parts[2]


def name_matches(members, query):
    user_id = query.strip().removeprefix("<@!").removeprefix("<@").removesuffix(">")
    if user_id.isdecimal():
        return [member for member in members if member.id == int(user_id)]
    query = query.casefold()
    exact = [member for member in members if query in {member.name.casefold(), member.display_name.casefold(), str(member).casefold()}]
    if exact:
        return exact[:25]
    names = {name.casefold() for member in members for name in (member.name, member.display_name)}
    close = set(get_close_matches(query, names, n=25, cutoff=0.6))
    return [member for member in members if member.name.casefold() in close or member.display_name.casefold() in close][:25]


def check_ban(guild, actor, member, bot_id):
    if not actor.guild_permissions.ban_members or not guild.me.guild_permissions.ban_members:
        raise ValueError("You and Kuro both need Ban Members permission.")
    if member.id in (guild.owner_id, bot_id, actor.id):
        raise ValueError("That member cannot be targeted.")
    if member.top_role >= guild.me.top_role or (actor.id != guild.owner_id and member.top_role >= actor.top_role):
        raise ValueError("That member is above your or Kuro's role authority.")


class VerifyModal(discord.ui.Modal, title="Kuro verification"):
    def __init__(self, features, user_id, guild_id):
        super().__init__(timeout=180)
        self.features = features
        self.user_id = user_id
        self.guild_id = guild_id
        self.a, self.b = secrets.randbelow(9) + 1, secrets.randbelow(9) + 1
        self.answer = discord.ui.TextInput(max_length=5)
        self.add_item(discord.ui.Label(text=f"What is {self.a} + {self.b}?", component=self.answer))
        self.deadline = time.monotonic() + 180
        self.used = False

    async def on_submit(self, interaction):
        if self.used or time.monotonic() > self.deadline or interaction.user.id != self.user_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This challenge expired. Press Verify again.", ephemeral=True)
            return
        self.used = True
        if self.answer.value.strip() != str(self.a + self.b):
            await interaction.response.send_message("Incorrect answer. Press Verify for a new challenge.", ephemeral=True)
            return
        config = self.features.store.option(interaction.guild_id, "verification")
        if not config:
            await interaction.response.send_message("Verification is no longer configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        member = await interaction.guild.fetch_member(interaction.user.id)
        if member.created_at > discord.utils.utcnow() - timedelta(days=config["min_days"]) and not self.features.store.option(interaction.guild_id,"verification_policy",{}).get("enabled"):
            await interaction.followup.send(f"Your account must be at least {config['min_days']} days old.", ephemeral=True)
            return
        if self.features.store.global_ban(interaction.guild_id, member.id) and not self.features.store.whitelisted(interaction.guild_id, member.id):
            await interaction.followup.send("A moderator needs to review your account before verification.", ephemeral=True)
            return
        extras = self.features.bot.get_cog("Wishlist") if hasattr(self.features.bot,"get_cog") else None
        if extras and (review_message := await extras.verification_gate(member)):
            await interaction.followup.send(review_message, ephemeral=True)
            return
        role = interaction.guild.get_role(config["role"])
        if not role or not self.features.safe_role(role, interaction.guild) or not interaction.guild.me.guild_permissions.manage_roles:
            await interaction.followup.send("The verification role needs moderator attention.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Kuro verification completed")
            await interaction.followup.send("Verified! Your role has been added.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Kuro couldn't add the role. Ask a moderator to check permissions.", ephemeral=True)

    async def on_error(self, interaction, error):
        log.warning("Verification failed: %s", type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send("Verification failed. Please try again.", ephemeral=True)
        else:
            await interaction.response.send_message("Verification failed. Please try again.", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self, features):
        super().__init__(timeout=None)
        self.features = features

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="kuro:verify:v1")
    async def verify(self, interaction, button):
        config = self.features.store.option(interaction.guild_id, "verification")
        if not config or interaction.channel_id != config["channel"]:
            await interaction.response.send_message("This verification panel is no longer active.", ephemeral=True)
            return
        key = (interaction.guild_id, interaction.user.id)
        now = time.monotonic()
        self.features.verify_cooldowns = {key: expiry for key, expiry in self.features.verify_cooldowns.items() if expiry > now}
        if key in self.features.verify_cooldowns:
            await interaction.response.send_message("Wait 30 seconds between challenges.", ephemeral=True)
            return
        self.features.verify_cooldowns[key] = now + 30
        await interaction.response.send_modal(VerifyModal(self.features, interaction.user.id, interaction.guild_id))


class DeathnoteChoice(discord.ui.View):
    def __init__(self, features, author_id, members, seconds, reason):
        super().__init__(timeout=90)
        self.features, self.author_id = features, author_id
        self.seconds, self.reason = seconds, reason
        select = discord.ui.Select(placeholder="Confirm the member to schedule", options=[
            discord.SelectOption(label=f"{member.display_name} (@{member.name})"[:100], value=str(member.id), description=f"User ID: {member.id}") for member in members
        ])
        select.callback = self.choose
        self.add_item(select)
        self.used = False
        self.lock = asyncio.Lock()

    async def choose(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the moderator who wrote this Deathnote can confirm it.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            if self.used:
                await interaction.followup.send("This Deathnote was already scheduled.", ephemeral=True)
                return
            try:
                actor = await interaction.guild.fetch_member(self.author_id)
                member = await interaction.guild.fetch_member(int(interaction.data["values"][0]))
                if self.features.store.option(interaction.guild_id, "deathnote_channel") != interaction.channel_id:
                    raise ValueError("This channel's Deathnotes are no longer enabled.")
                check_ban(interaction.guild, actor, member, self.features.bot.user.id)
                due = int(time.time()) + self.seconds
                job = self.features.store.schedule(interaction.guild_id, member.id, due, self.reason, actor.id)
                self.used = True
                await interaction.followup.send(f"Deathnote #{job}: {member.mention}, <t:{due}:R>.", ephemeral=True, allowed_mentions=NO_MENTIONS)
                self.stop()
                try:
                    await interaction.message.edit(content=f"Deathnote #{job} scheduled for user {member.id}.", view=None)
                except discord.HTTPException:
                    pass
            except (ValueError, discord.HTTPException) as exc:
                await interaction.followup.send(str(exc)[:1500], ephemeral=True, allowed_mentions=NO_MENTIONS)


class Features(commands.Cog):
    def __init__(self, bot, store, safe_role):
        self.bot, self.store, self.safe_role = bot, store, safe_role
        self.verify_cooldowns = {}
        self.recovery_locks = {}
        self.chat_cooldowns = {}
        self.reconciled = False
        self.files = bot.data_dir / "attachments"
        self.files.mkdir(parents=True, exist_ok=True)
        self.file_bytes = sum(path.stat().st_size for path in self.files.iterdir() if path.is_file())

    async def cog_load(self):
        self.bot.add_view(VerifyView(self))
        self.healing.start()

    async def cog_unload(self):
        self.healing.cancel()

    @tasks.loop(seconds=15)
    async def healing(self):
        for guild in self.bot.guilds:
            try:
                await self.reconcile(guild)
            except Exception as exc:
                log.error("Recovery tick failed in server %s: %s",guild.id,type(exc).__name__)

    @healing.before_loop
    async def before_healing(self):
        await self.bot.wait_until_ready()

    def require_content(self):
        if not self.bot.intents.message_content:
            raise ValueError("Enable Message Content Intent in Discord's Developer Portal, then set MESSAGE_CONTENT_ENABLED=true on Railway and restart Kuro.")

    def check_channel(self, interaction, channel):
        if channel.guild.id != interaction.guild_id or not channel.permissions_for(interaction.user).view_channel:
            raise ValueError("Choose a channel in this server that you can access.")
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.view_channel or not perms.send_messages:
            raise ValueError("Kuro needs View Channel and Send Messages in that channel.")

    @app_commands.command(description="Post a persistent verification button and set its member role.")
    @manager
    async def verification_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role, min_account_days: app_commands.Range[int, 0, 365] = 0):
        self.check_channel(interaction, channel)
        if not self.safe_role(role, interaction.guild) or not interaction.guild.me.guild_permissions.manage_roles:
            raise ValueError("Choose an ordinary role below Kuro's role, and give Kuro Manage Roles.")
        if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
            raise ValueError("The verification role must be below your highest role.")
        if self.store.settings(interaction.guild_id)["autorole"] == role.id:
            raise ValueError("This role is already the join autorole. Disable that autorole before using it for verification.")
        if role.id==(self.store.option(interaction.guild_id,"jail") or {}).get("role"):
            raise ValueError("The verified role must be separate from the jail role.")
        await interaction.response.defer(ephemeral=True)
        panel = await channel.send("**Welcome!** Press Verify and complete the short challenge to receive your member role.", view=VerifyView(self))
        self.store.set_option(interaction.guild_id, "verification", {"channel": channel.id, "role": role.id, "min_days": min_account_days, "panel": panel.id})
        await interaction.followup.send("Verification panel posted. Configure your server's channel permissions so the verified role unlocks member channels.", ephemeral=True)

    @app_commands.command(description="Disable verification. Existing member roles are kept.")
    @manager
    async def verification_disable(self, interaction: discord.Interaction):
        self.store.set_option(interaction.guild_id, "verification", None)
        await interaction.response.send_message("Verification disabled. Existing panels will no longer work.", ephemeral=True)

    @app_commands.command(description="Enable or disable name-based Deathnotes in a text channel.")
    @manager
    async def deathnote_channel(self, interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool = True):
        self.check_channel(interaction, channel)
        if enabled:
            self.require_content()
            if self.store.db.execute("SELECT 1 FROM chat_channels WHERE channel=?", (channel.id,)).fetchone():
                raise ValueError("Unlink this channel from shared chat first.")
        self.store.set_option(interaction.guild_id, "deathnote_channel", channel.id if enabled else None)
        await interaction.response.send_message("Deathnote channel enabled. Write `name or @member | 10m | reason`, then choose the matching member to confirm." if enabled else "Deathnote channel disabled.", ephemeral=True)

    @app_commands.command(description="Create a shared-ban network controlled by this server.")
    @manager
    async def network_create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 60]):
        require_owner(interaction)
        self.store.create_network(interaction.guild_id, name)
        await interaction.response.send_message(f"Created {discord.utils.escape_markdown(name)}. Use /network_invite to invite another server's owner.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    def home_network(self, interaction):
        network = self.store.network(interaction.guild_id)
        if not network or network["home"] != interaction.guild_id:
            raise ValueError("Manage shared bans from the network's founding server.")
        return network

    @app_commands.command(description="Create a single-use, one-hour invitation to this ban network.")
    @manager
    async def network_invite(self, interaction: discord.Interaction):
        require_owner(interaction)
        network = self.home_network(interaction)
        code = self.store.invite("network", network["id"])
        await interaction.response.send_message(f"Send this code to the other server's owner: `{code}`. They use /network_join. Joining applies the network's current and future bans unless locally whitelisted.", ephemeral=True)

    @app_commands.command(description="Join a shared-ban network using its owner's invitation.")
    @manager
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def network_join(self, interaction: discord.Interaction, code: str):
        require_owner(interaction)
        await interaction.response.defer(ephemeral=True)
        network = self.store.join_network(interaction.guild_id, code)
        rows = self.store.db.execute("SELECT * FROM global_bans WHERE network=?", (network,)).fetchall()
        results = [await self.enforce_ban(interaction.guild, row) for row in rows]
        await interaction.followup.send(f"Joined the network. Existing bans checked: {len(results)}; applied: {results.count('banned')}. Use /globalban_list to review results.", ephemeral=True)

    @app_commands.command(description="Leave a ban network; leaving its founding server dissolves the network.")
    @manager
    async def network_leave(self, interaction: discord.Interaction):
        require_owner(interaction)
        self.store.leave_network(interaction.guild_id)
        await interaction.response.send_message("Network membership removed. Existing Discord bans remain; no future shared bans will apply here.", ephemeral=True)

    async def enforce_ban(self, guild, row):
        member_id = row["member"]
        try:
            if self.store.whitelisted(guild.id, member_id):
                result = "whitelisted"
            elif member_id in (guild.owner_id, self.bot.user.id, row["actor"]):
                result = "protected"
            elif not guild.me.guild_permissions.ban_members:
                result = "missing Ban Members"
            else:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.NotFound:
                    member = None
                if member and member.top_role >= guild.me.top_role:
                    result = "role hierarchy blocked"
                elif self.store.whitelisted(guild.id,member_id):
                    result = "whitelisted"
                else:
                    await guild.ban(discord.Object(id=member_id), reason=f"Kuro shared ban by {row['actor']}: {row['reason']}"[:512], delete_message_seconds=0)
                    result = "banned"
        except discord.HTTPException as exc:
            result = f"Discord error {exc.status}"
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO ban_results VALUES (?,?,?,?)", (row["network"], member_id, guild.id, result))
        return result

    @app_commands.command(description="Add a user ID to this network's ban list and apply it to member servers.")
    @moderator
    async def globalban_add(self, interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 1, 400]):
        network = self.home_network(interaction)
        if not user_id.isdecimal() or not 0 < int(user_id) < 2**63:
            raise ValueError("Enter a valid Discord user ID.")
        member_id = int(user_id)
        if member_id in (interaction.guild.owner_id, self.bot.user.id, interaction.user.id):
            raise ValueError("That user cannot be targeted.")
        await interaction.response.defer(ephemeral=True)
        try:
            member = await interaction.guild.fetch_member(member_id)
        except discord.NotFound:
            member = None
        actor = await interaction.guild.fetch_member(interaction.user.id)
        if member:
            check_ban(interaction.guild, actor, member, self.bot.user.id)
        elif not actor.guild_permissions.ban_members:
            raise ValueError("You need Ban Members permission.")
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO global_bans VALUES (?,?,?,?)", (network["id"], member_id, reason, actor.id))
        row = self.store.global_ban(interaction.guild_id, member_id)
        subscriptions = self.store.db.execute("SELECT guild FROM subscriptions WHERE network=?", (network["id"],)).fetchall()
        results = []
        for subscription in subscriptions:
            guild = self.bot.get_guild(subscription["guild"])
            result = await self.enforce_ban(guild, row) if guild else "bot not in server"
            results.append(f"{guild.name if guild else subscription['guild']}: {result}")
        await interaction.followup.send(("Shared ban saved.\n" + "\n".join(results))[:1900], ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Remove a shared ban entry. Existing Discord bans remain until manually unbanned.")
    @moderator
    async def globalban_remove(self, interaction: discord.Interaction, user_id: str):
        network = self.home_network(interaction)
        if not user_id.isdecimal() or not 0 < int(user_id) < 2**63:
            raise ValueError("Enter a valid Discord user ID.")
        with self.store.db:
            self.store.db.execute("DELETE FROM global_bans WHERE network=? AND member=?", (network["id"], int(user_id)))
            self.store.db.execute("DELETE FROM ban_results WHERE network=? AND member=?", (network["id"], int(user_id)))
        await interaction.response.send_message("Shared ban entry removed. Existing Discord bans must be manually lifted in each server.", ephemeral=True)

    @app_commands.command(description="View shared bans and their enforcement result in this server.")
    @moderator
    async def globalban_list(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 10000] = 1):
        network = self.store.network(interaction.guild_id)
        if not network:
            raise ValueError("This server is not in a ban network.")
        rows = self.store.db.execute("SELECT b.member,b.reason,r.result FROM global_bans b LEFT JOIN ban_results r ON r.network=b.network AND r.member=b.member AND r.guild=? WHERE b.network=? ORDER BY b.member LIMIT 10 OFFSET ?", (interaction.guild_id, network["id"], (page-1)*10)).fetchall()
        lines = [f"{row['member']}: {row['result'] or 'not checked'} — {row['reason'][:100]}" for row in rows]
        await interaction.response.send_message(("\n".join(lines) or "No shared ban entries on this page.")[:1950], ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Exempt a user ID from shared bans in this server, or remove that exemption.")
    @manager
    async def whitelist(self, interaction: discord.Interaction, user_id: str, enabled: bool):
        if not user_id.isdecimal() or not 0 < int(user_id) < 2**63:
            raise ValueError("Enter a valid Discord user ID.")
        self.store.set_whitelist(interaction.guild_id, int(user_id), enabled)
        await interaction.response.defer(ephemeral=True)
        result = "Exemption saved. Existing Discord bans are not automatically lifted."
        if not enabled and (row := self.store.global_ban(interaction.guild_id, int(user_id))):
            result = "Exemption removed. Shared ban result: " + await self.enforce_ban(interaction.guild, row)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(description="Invite another server to link a public chat channel with this one.")
    @manager
    async def chat_invite(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.require_content()
        self.check_channel(interaction, channel)
        if self.store.option(interaction.guild_id, "deathnote_channel") == channel.id:
            raise ValueError("Choose a different channel from the Deathnote channel.")
        if not channel.permissions_for(interaction.guild.default_role).view_channel:
            raise ValueError("Shared chat requires a public channel visible to @everyone.")
        row = self.store.db.execute("SELECT room FROM chat_channels WHERE channel=?", (channel.id,)).fetchone()
        room = row[0] if row else secrets.token_hex(12)
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO chat_channels VALUES (?,?,?)", (channel.id, interaction.guild_id, room))
        code = self.store.invite("chat", room)
        await interaction.response.send_message(f"Link invitation: `{code}` (one use, one hour). The other server's manager uses /chat_join. Messages and attachment links from linked channels will be shared.", ephemeral=True)

    @app_commands.command(description="Link this public channel to shared chat using an invitation.")
    @manager
    async def chat_join(self, interaction: discord.Interaction, channel: discord.TextChannel, code: str):
        self.require_content()
        self.check_channel(interaction, channel)
        if not channel.permissions_for(interaction.guild.default_role).view_channel:
            raise ValueError("Shared chat requires a public channel visible to @everyone.")
        if self.store.option(interaction.guild_id, "deathnote_channel") == channel.id:
            raise ValueError("Choose a different channel from the Deathnote channel.")
        if self.store.db.execute("SELECT 1 FROM chat_channels WHERE channel=?", (channel.id,)).fetchone():
            raise ValueError("Unlink this channel before joining another chat.")
        room = self.store.consume_invite("chat", code)
        if not self.store.db.execute("SELECT 1 FROM chat_channels WHERE room=?", (room,)).fetchone():
            raise ValueError("That shared chat no longer exists.")
        with self.store.db:
            self.store.db.execute("INSERT INTO chat_channels VALUES (?,?,?)", (channel.id, interaction.guild_id, room))
        await interaction.response.send_message("Shared chat linked. Messages in this channel will now be relayed to the linked channels.", ephemeral=True)

    @app_commands.command(description="Stop relaying chat from this channel.")
    @manager
    async def chat_leave(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.check_channel(interaction, channel)
        with self.store.db:
            self.store.db.execute("DELETE FROM chat_channels WHERE channel=? AND guild=?", (channel.id, interaction.guild_id))
        await interaction.response.send_message("Channel unlinked. Previously relayed messages remain in the other servers.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if (row := self.store.global_ban(member.guild.id, member.id)):
            await self.enforce_ban(member.guild, row)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.bot.intents.message_content or not message.guild or message.author.id == self.bot.user.id:
            return
        include_bots = self.store.option(message.guild.id,"recovery_plus",{}).get("bots",False)
        if self.store.option(message.guild.id, "recovery", {}).get("archive") and (not message.author.bot or include_bots):
            await self.archive_message(message)
        if message.author.bot or message.webhook_id:
            return
        if self.store.option(message.guild.id, "deathnote_channel") == message.channel.id:
            if not message.author.guild_permissions.ban_members:
                return
            try:
                query, seconds, reason = parse_deathnote(message.content)
                members = name_matches(message.guild.members, query)
                if not members:
                    raise ValueError("No matching cached member found. Try their @mention or /deathnote.")
                view = DeathnoteChoice(self, message.author.id, members, seconds, reason)
                await message.reply("Choose the member below to confirm this scheduled ban:", view=view, mention_author=False, allowed_mentions=NO_MENTIONS)
            except ValueError as exc:
                await message.reply(str(exc), mention_author=False, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.warning("Could not reply in Deathnote channel %s", message.channel.id)
            return
        await self.relay_message(message)

    async def relay_message(self, message):
        row = self.store.db.execute("SELECT room FROM chat_channels WHERE channel=?", (message.channel.id,)).fetchone()
        if not row or not isinstance(message.channel, discord.TextChannel):
            return
        # Stop forwarding immediately if a linked channel becomes private.
        if not message.channel.permissions_for(message.guild.default_role).view_channel:
            return
        key, now = (message.guild.id, message.author.id), time.monotonic()
        self.chat_cooldowns = {key: expiry for key, expiry in self.chat_cooldowns.items() if expiry > now}
        if key in self.chat_cooldowns:
            return
        self.chat_cooldowns[key] = now + 1
        header = discord.utils.escape_markdown(f"[{message.guild.name} / #{message.channel.name}] {message.author.display_name} (@{message.author.name})")
        body = message.content
        if message.attachments:
            body += "\n" + "\n".join(attachment.url for attachment in message.attachments)
        if not body.strip():
            return
        content = (header[:300] + "\n" + body)[:2000]
        rows = self.store.db.execute("SELECT channel FROM chat_channels WHERE room=? AND channel!=?", (row[0], message.channel.id)).fetchall()
        for row in rows:
            target = self.bot.get_channel(row[0])
            if not isinstance(target, discord.TextChannel) or not target.permissions_for(target.guild.default_role).view_channel:
                continue
            try:
                await target.send(content, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
            except discord.HTTPException:
                log.warning("Shared chat delivery failed for channel %s", row[0])

    async def archive_message(self, message):
        if not isinstance(message.channel, (discord.TextChannel,discord.Thread)):
            return
        if isinstance(message.channel,discord.Thread) and message.channel.is_private():
            return
        attachments = []
        for attachment in message.attachments:
            item = {"name": attachment.filename, "url": attachment.url, "size": attachment.size}
            path = self.files / f"{message.id}-{attachment.id}"
            if path.exists():
                item["file"] = path.name
            elif attachment.size <= 8 * 1024**2 and self.file_bytes + attachment.size <= 256 * 1024**2:
                try:
                    data = await attachment.read()
                    # Recheck after the await: other channels may have used the budget.
                    if path.exists():
                        item["file"] = path.name
                    elif self.file_bytes + len(data) <= 256 * 1024**2:
                        path.write_bytes(data)
                        self.file_bytes += len(data)
                        item["file"] = path.name
                except (discord.HTTPException, OSError):
                    log.warning("Could not archive attachment %s", attachment.id)
            attachments.append(item)
        payload = {"id": message.id, "author": str(message.author), "author_id": message.author.id, "created": message.created_at.isoformat(), "content": message.content, "attachments": attachments, "embeds": [embed.to_dict() for embed in message.embeds]}
        removed = self.store.archive(message.guild.id, message.channel.id, message.id, int(message.created_at.timestamp()), payload)
        # Prune evicted attachments and files removed by an observed message edit.
        kept_files = {item.get("file") for item in attachments}
        for item in removed:
            for attachment in item.get("attachments", []):
                filename = attachment.get("file", "")
                if filename not in kept_files and re.fullmatch(r"\d+-\d+", filename):
                    path = self.files / filename
                    if path.exists():
                        self.file_bytes -= path.stat().st_size
                        path.unlink()

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if self.bot.intents.message_content and after.guild and after.author.id != self.bot.user.id and (not after.author.bot or self.store.option(after.guild.id,"recovery_plus",{}).get("bots",False)) and self.store.option(after.guild.id, "recovery", {}).get("archive"):
            await self.archive_message(after)

    def channel_snapshot(self, channel):
        if not isinstance(channel, (discord.CategoryChannel, discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
            return None
        data = {"name": channel.name, "type": channel.type.value, "position": channel.position, "category": getattr(channel, "category_id", None), "overwrites": []}
        for target, overwrite in channel.overwrites.items():
            allow, deny = overwrite.pair()
            data["overwrites"].append({"id": target.id, "role": isinstance(target, discord.Role) or (isinstance(target, discord.Object) and target.type is discord.Role), "allow": allow.value, "deny": deny.value})
        for attribute in ("topic", "nsfw", "slowmode_delay", "bitrate", "user_limit", "default_auto_archive_duration", "default_thread_slowmode_delay"):
            if hasattr(channel, attribute):
                data[attribute] = getattr(channel, attribute)
        if isinstance(channel, discord.ForumChannel):
            data["tags"] = [{"name": tag.name, "moderated": tag.moderated, "emoji": str(tag.emoji) if tag.emoji else None} for tag in channel.available_tags]
        return data

    @app_commands.command(description="Enable channel recovery, with optional recent-message and attachment backups.")
    @manager
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def recovery_setup(self, interaction: discord.Interaction, enabled: bool, archive_messages: bool = False):
        if archive_messages and enabled:
            self.require_content()
        await interaction.response.defer(ephemeral=True)
        lock = self.recovery_locks.setdefault(interaction.guild_id, asyncio.Lock())
        async with lock:
            if enabled:
                channels = await interaction.guild.fetch_channels()
                with self.store.db:
                    self.store.db.execute("DELETE FROM channel_snapshots WHERE guild=?", (interaction.guild_id,))
                    self.store.db.execute("DELETE FROM recovery_jobs WHERE guild=?", (interaction.guild_id,))
                for channel in channels:
                    if (snapshot := self.channel_snapshot(channel)):
                        self.store.snapshot(interaction.guild_id, channel.id, snapshot)
            self.store.set_option(interaction.guild_id, "recovery", {"enabled": enabled, "archive": archive_messages and enabled})
        await interaction.followup.send("Recovery enabled. New channels and category moves are saved automatically. Missing categories are recreated first, then their channels, one recovery step every 15 seconds. /recovery_status shows the queue. Message backup limits are configurable with /recovery_plus setup." if enabled else "Recovery disabled. Channels may now be intentionally deleted without recreation. Saved backups are retained.", ephemeral=True)

    async def restore_channel(self, guild, old_id, snapshot):
        overwrites = {}
        for item in snapshot["overwrites"]:
            target = guild.get_role(item["id"]) if item["role"] else discord.Object(id=item["id"])
            if target is None:
                # A missing private role must not silently turn a channel public.
                raise ValueError("A permission role is missing; restore the role before restoring this channel.")
            overwrites[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(item["allow"]), discord.Permissions(item["deny"]))
        common = {"name": snapshot["name"], "overwrites": overwrites, "position": snapshot["position"], "reason": "Kuro self-healing recovery"}
        kind = snapshot["type"]
        if kind == discord.ChannelType.category.value:
            created = await guild.create_category(**common)
        else:
            category_id = snapshot.get("category")
            category = guild.get_channel(category_id) if category_id else None
            if category_id and category is None:
                raise ValueError("Parent category is not restored yet.")
            common["category"] = category
            if kind in (discord.ChannelType.text.value, discord.ChannelType.news.value):
                options = {key: snapshot[key] for key in ("topic", "nsfw", "slowmode_delay", "default_auto_archive_duration", "default_thread_slowmode_delay") if key in snapshot}
                created = await guild.create_text_channel(**common, news=kind == discord.ChannelType.news.value, **options)
            elif kind in (discord.ChannelType.voice.value, discord.ChannelType.stage_voice.value):
                options = {key: snapshot[key] for key in ("bitrate", "user_limit", "nsfw") if key in snapshot}
                creator = guild.create_stage_channel if kind == discord.ChannelType.stage_voice.value else guild.create_voice_channel
                created = await creator(**common, **options)
            elif kind in (discord.ChannelType.forum.value, discord.ChannelType.media.value):
                options = {key: snapshot[key] for key in ("topic", "nsfw", "slowmode_delay", "default_auto_archive_duration", "default_thread_slowmode_delay") if key in snapshot}
                tags = [discord.ForumTag(**tag) for tag in snapshot.get("tags", [])]
                created = await guild.create_forum(**common, media=kind == discord.ChannelType.media.value, available_tags=tags, **options)
            else:
                return None
        self.store.remap_channel(guild.id, old_id, created.id)
        extras = self.bot.get_cog("Wishlist") if hasattr(self.bot,"get_cog") else None
        if extras:
            extras.state.remap_channel(guild.id,old_id,created.id)
        self.store.snapshot(guild.id, created.id, self.channel_snapshot(created))
        if isinstance(created, discord.TextChannel):
            await self.replay_messages(created)
            config = self.store.option(guild.id, "verification")
            if config and config["channel"] == created.id:
                panel = await created.send("**Verification** — press Verify to receive your member role.", view=VerifyView(self))
                config["panel"] = panel.id
                self.store.set_option(guild.id, "verification", config)
        log.info("Recovered channel %s as %s in server %s", old_id, created.id, guild.id)
        return created

    async def replay_messages(self, channel):
        messages = self.store.messages(channel.guild.id, channel.id)
        replay = self.store.option(channel.guild.id,"recovery_plus",{}).get("replay",50)
        if not messages or replay == 0:
            return
        try:
            await channel.send(f"**Kuro recovery:** replaying the latest {min(replay, len(messages))} saved messages. These are archive copies; original message IDs and authorship cannot be restored.", allowed_mentions=NO_MENTIONS)
            for message in messages[-replay:]:
                header = discord.utils.escape_markdown(f"{message['author']} · {message['created']}")
                files, links = [], []
                for attachment in message.get("attachments", []):
                    filename = attachment.get("file", "")
                    path = self.files / filename
                    if re.fullmatch(r"\d+-\d+", filename) and path.is_file() and path.stat().st_size <= channel.guild.filesize_limit:
                        files.append(discord.File(path, filename=attachment["name"]))
                    else:
                        links.append(attachment["url"])
                body = f"**{header[:300]}**\n{message['content']}"
                if links:
                    body += "\n" + "\n".join(links)
                try:
                    await channel.send(body[:2000], files=files, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
                finally:
                    for file in files:
                        file.close()
        except discord.HTTPException:
            log.warning("History replay incomplete for channel %s; saved export remains available", channel.id)

    async def reconcile(self, guild):
        lock = self.recovery_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if not self.store.option(guild.id, "recovery", {}).get("enabled"):
                return
            extras = self.bot.get_cog("Wishlist") if hasattr(self.bot,"get_cog") else None
            if extras:
                await extras.restore_roles(guild)
            # A fresh REST inventory prevents cache gaps on reconnect from cloning real channels.
            try:
                existing = {channel.id: channel for channel in await guild.fetch_channels()}
            except discord.HTTPException:
                log.warning("Recovery inventory failed for server %s", guild.id)
                return
            snapshots = sorted(self.store.snapshots(guild.id), key=lambda item: (item[1]["type"] != discord.ChannelType.category.value, item[1].get("category") or 0, item[1]["position"]))
            with self.store.db:
                for channel_id,snapshot in snapshots:
                    if channel_id not in existing:
                        self.store.db.execute("INSERT OR IGNORE INTO recovery_jobs(guild,channel) VALUES (?,?)",(guild.id,channel_id))
            attempted = False
            for old_id, snapshot in snapshots:
                # Category restoration may have remapped this snapshot since inventory.
                current = dict(self.store.snapshots(guild.id)).get(old_id, snapshot)
                if old_id in existing:
                    channel = existing[old_id]
                    parent = current.get("category")
                    if parent and getattr(channel, "category_id", None) != parent and guild.get_channel(parent):
                        try:
                            await channel.edit(category=guild.get_channel(parent), reason="Kuro recovered parent category")
                            attempted = True
                        except discord.HTTPException:
                            log.warning("Could not restore channel %s category", old_id)
                        break
                    continue
                job = self.store.db.execute("SELECT * FROM recovery_jobs WHERE guild=? AND channel=?",(guild.id,old_id)).fetchone()
                if job and job["retry_at"] > time.time():
                    continue
                attempted = True
                try:
                    created = await self.restore_channel(guild, old_id, current)
                    with self.store.db:
                        self.store.db.execute("UPDATE recovery_jobs SET status='restored',replacement=?,error=NULL WHERE guild=? AND channel=?",(created.id,guild.id,old_id))
                except (discord.HTTPException, ValueError) as exc:
                    log.warning("Channel %s recovery failed: %s", old_id, str(exc)[:200])
                    attempts = (job["attempts"] if job else 0)+1
                    with self.store.db:
                        self.store.db.execute("UPDATE recovery_jobs SET status='waiting',attempts=?,error=?,retry_at=? WHERE guild=? AND channel=?",(attempts,str(exc)[:300],int(time.time())+min(900,15*2**min(attempts,6)),guild.id,old_id))
                break
            if extras and not attempted:
                await extras.restore_threads(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.reconcile(guild)

    @commands.Cog.listener()
    async def on_resumed(self):
        for guild in self.bot.guilds:
            await self.reconcile(guild)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if self.store.option(channel.guild.id,"recovery",{}).get("enabled"):
            # Queue only. Rebuilding in the event handler would race a mass deletion.
            if channel.id not in dict(self.store.snapshots(channel.guild.id)) and (snapshot:=self.channel_snapshot(channel)):
                self.store.snapshot(channel.guild.id,channel.id,snapshot)
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO recovery_jobs(guild,channel) VALUES (?,?)",(channel.guild.id,channel.id))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        lock = self.recovery_locks.setdefault(channel.guild.id, asyncio.Lock())
        async with lock:
            if self.store.option(channel.guild.id, "recovery", {}).get("enabled") and (snapshot := self.channel_snapshot(channel)):
                if self.store.option(channel.guild.id,"recovery_plus",{}).get("roles"):
                    previous=dict(self.store.snapshots(channel.guild.id)).get(channel.id,{})
                    present={item["id"] for item in snapshot["overwrites"]}
                    for overwrite in previous.get("overwrites",[]):
                        if overwrite["role"] and overwrite["id"] not in present and channel.guild.get_role(overwrite["id"]) is None:
                            snapshot["overwrites"].append(overwrite)
                self.store.snapshot(channel.guild.id, channel.id, snapshot)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # Discord removes a deleted category from surviving children. Preserve
        # their previous grouping so reconciliation can reattach them.
        if getattr(before, "category_id", None) and not getattr(after, "category_id", None) and not after.guild.get_channel(before.category_id):
            return
        await self.on_guild_channel_create(after)

    @app_commands.command(description="Show Kuro's enabled features and storage status for this server.")
    @manager
    async def features_status(self, interaction: discord.Interaction):
        network = self.store.network(interaction.guild_id)
        recovery = self.store.option(interaction.guild_id, "recovery", {})
        chat_count = self.store.db.execute("SELECT count(*) FROM chat_channels WHERE guild=?", (interaction.guild_id,)).fetchone()[0]
        lines = [
            f"Message Content Intent: {'on' if self.bot.intents.message_content else 'off'}",
            f"Verification: {'configured' if self.store.option(interaction.guild_id, 'verification') else 'off'}",
            f"Deathnote channel: {self.store.option(interaction.guild_id, 'deathnote_channel') or 'off'}",
            f"Ban network: {network['name'] if network else 'none'}",
            f"Shared chat channels: {chat_count}",
            f"Channel recovery: {'on' if recovery.get('enabled') else 'off'}",
            f"New message backups: {'on' if recovery.get('archive') else 'off'}",
            f"Jail role: {(self.store.option(interaction.guild_id,'jail') or {}).get('role','off')}",
            f"Activity XP: {'on' if self.store.option(interaction.guild_id,'levels',{}).get('enabled') else 'off'}",
            f"Verification review: {'on' if self.store.option(interaction.guild_id,'verification_policy',{}).get('enabled') else 'off'}",
            f"Role recovery: {'on' if self.store.option(interaction.guild_id,'recovery_plus',{}).get('roles') else 'off'}",
            f"Thread recovery: {'on' if self.store.option(interaction.guild_id,'recovery_plus',{}).get('threads') else 'off'}",
            f"Owner lockdown: {'on' if self.store.option(interaction.guild_id,'lockdown',False) else 'off'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Retry recovery for channels that could not previously be restored.")
    @manager
    async def recovery_retry(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        with self.store.db:
            self.store.db.execute("UPDATE recovery_jobs SET retry_at=0 WHERE guild=? AND status!='restored'",(interaction.guild_id,))
        await self.reconcile(interaction.guild)
        await interaction.followup.send("Recovery retried one step. Remaining missing channels stay queued; check /recovery_status.", ephemeral=True)

    @app_commands.command(description="See missing channels grouped by category and recent recovery failures.")
    @manager
    async def recovery_status(self,interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        actual={channel.id for channel in await interaction.guild.fetch_channels()}
        snapshots=dict(self.store.snapshots(interaction.guild_id))
        groups={}
        for channel_id,snapshot in snapshots.items():
            if channel_id not in actual:
                parent=snapshot.get("category")
                name=snapshots.get(parent,{}).get("name","Uncategorized") if snapshot["type"]!=4 else "Missing categories"
                groups[name]=groups.get(name,0)+1
        errors=self.store.db.execute("SELECT channel,error FROM recovery_jobs WHERE guild=? AND status='waiting' ORDER BY retry_at LIMIT 5",(interaction.guild_id,)).fetchall()
        lines=["Recovery: "+("on" if self.store.option(interaction.guild_id,"recovery",{}).get("enabled") else "off"),f"Saved channels and categories: {len(snapshots)}"]
        lines += [f"{name}: {count} missing" for name,count in groups.items()]
        lines += [f"Channel {row['channel']}: {row['error']}" for row in errors]
        if not groups:
            lines.append("All saved channels are present.")
        await interaction.followup.send("\n".join(lines)[:1900],ephemeral=True,allowed_mentions=NO_MENTIONS)

    @app_commands.command(description="Export saved text, embed data, and attachment references from a channel.")
    @manager
    async def backup_export(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.check_channel(interaction, channel)
        if not channel.permissions_for(interaction.user).read_message_history:
            raise ValueError("You need Read Message History in that channel.")
        await interaction.response.defer(ephemeral=True)
        data = json.dumps(self.store.messages(interaction.guild_id, channel.id), ensure_ascii=False, indent=2).encode()
        if len(data) > interaction.guild.filesize_limit:
            raise ValueError("This export exceeds the server's upload limit. Retrieve the database from Kuro's volume instead.")
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename=f"kuro-backup-{channel.id}.json"), ephemeral=True)
