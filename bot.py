import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from storage import Store, duration_seconds

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("kuro")
store = Store(str(ROOT / "data" / "kuro.sqlite3"))


def safe_role(role: discord.Role, guild: discord.Guild) -> bool:
    permissions = role.permissions
    return (not role.is_default() and not role.managed and role < guild.me.top_role
            and not any((permissions.administrator, permissions.manage_guild,
                         permissions.manage_roles, permissions.manage_channels,
                         permissions.ban_members, permissions.kick_members,
                         permissions.manage_webhooks, permissions.moderate_members)))


class Kuro(commands.Bot):
    async def setup_hook(self):
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.execute_bans.start()

    @tasks.loop(seconds=15)
    async def execute_bans(self):
        for job in store.pending():
            if job["due"] > int(time.time()):
                break
            guild = self.get_guild(job["guild"])
            if guild is None:
                store.finish(job["id"], "failed", "Bot is no longer in the server.")
                continue
            try:
                # Recheck authority at execution time; roles can change during a delay.
                actor = await guild.fetch_member(job["actor"])
                try:
                    member = await guild.fetch_member(job["member"])
                except discord.NotFound:
                    member = None
                if not actor.guild_permissions.ban_members or not guild.me.guild_permissions.ban_members:
                    raise ValueError("Ban permission is no longer available.")
                if job["member"] in (guild.owner_id, self.user.id, actor.id):
                    raise ValueError("Protected target.")
                if member and (member.top_role >= guild.me.top_role or
                               (actor.id != guild.owner_id and member.top_role >= actor.top_role)):
                    raise ValueError("Target is above the allowed role hierarchy.")
                await guild.ban(discord.Object(id=job["member"]),
                                reason=f"Deathnote #{job['id']} by {actor.id}: {job['reason']}"[:512],
                                delete_message_seconds=0)
                store.finish(job["id"], "completed")
                log.info("Deathnote #%s completed in server %s", job["id"], guild.id)
            except (discord.HTTPException, ValueError) as exc:
                store.finish(job["id"], "failed", str(exc)[:500])
                log.warning("Deathnote #%s failed: %s", job["id"], type(exc).__name__)

    @execute_bans.before_loop
    async def ready_for_bans(self):
        await self.wait_until_ready()


intents = discord.Intents.default()
intents.members = True
bot = Kuro(command_prefix=commands.when_mentioned, intents=intents)


@bot.event
async def on_ready():
    log.info("Kuro connected as %s; serving %s server(s)", bot.user, len(bot.guilds))


@bot.tree.command(description="Check whether Kuro is awake.")
@app_commands.guild_only()
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Kuro is awake. {round(bot.latency * 1000)} ms.", ephemeral=True)


@bot.tree.command(description="Schedule a ban. Delay examples: 10m, 2h, 3d.")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def deathnote(interaction: discord.Interaction, member: discord.Member, delay: str, reason: app_commands.Range[str, 1, 400]):
    guild = interaction.guild
    actor = interaction.user
    if member.id in (guild.owner_id, bot.user.id, actor.id) or member.top_role >= guild.me.top_role or (actor.id != guild.owner_id and member.top_role >= actor.top_role):
        await interaction.response.send_message("That member is outside your or Kuro's ban authority.", ephemeral=True)
        return
    seconds = duration_seconds(delay)
    due = int(time.time()) + seconds
    job = store.schedule(guild.id, member.id, due, reason, actor.id)
    await interaction.response.send_message(f"Deathnote #{job}: {member.mention} will be banned <t:{due}:R>. Reason: {reason}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="Cancel a pending Deathnote in this server.")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
async def deathnote_cancel(interaction: discord.Interaction, number: int):
    cancelled = store.cancel(interaction.guild_id, number)
    await interaction.response.send_message("Deathnote cancelled." if cancelled else "No pending Deathnote with that number in this server.", ephemeral=True)


@bot.tree.command(description="View the latest scheduled ban results and pending bans.")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
async def deathnote_list(interaction: discord.Interaction):
    rows = store.db.execute("SELECT * FROM scheduled_bans WHERE guild=? ORDER BY id DESC LIMIT 10", (interaction.guild_id,)).fetchall()
    lines = [f"#{row['id']} · <@{row['member']}> · {row['status']} · <t:{row['due']}:R>" + (f" · {row['error']}" if row['error'] else "") for row in rows]
    await interaction.response.send_message(("\n".join(lines) or "No Deathnotes yet.")[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="Configure the join role and restoration of ordinary roles after rejoining.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
async def roles_setup(interaction: discord.Interaction, sticky: bool, autorole: discord.Role | None = None):
    if autorole and (not safe_role(autorole, interaction.guild) or (interaction.user.id != interaction.guild.owner_id and autorole >= interaction.user.top_role)):
        await interaction.response.send_message("Choose an ordinary role below both your and Kuro's highest roles.", ephemeral=True)
        return
    store.configure(interaction.guild_id, autorole.id if autorole else None, sticky)
    await interaction.response.send_message(f"Saved. Join role: {autorole.mention if autorole else 'off'}. Sticky roles: {'on' if sticky else 'off'}.", ephemeral=True)


@bot.event
async def on_member_remove(member: discord.Member):
    if store.settings(member.guild.id)["sticky"]:
        store.remember_roles(member.guild.id, member.id, [role.id for role in member.roles if safe_role(role, member.guild)])


@bot.event
async def on_member_join(member: discord.Member):
    settings = store.settings(member.guild.id)
    ids = set(store.roles(member.guild.id, member.id)) if settings["sticky"] else set()
    if settings["autorole"]:
        ids.add(settings["autorole"])
    roles = [role for role_id in ids if (role := member.guild.get_role(role_id)) and safe_role(role, member.guild)]
    if roles:
        try:
            await member.add_roles(*roles, reason="Kuro join / sticky roles")
        except discord.HTTPException:
            log.warning("Could not assign join roles in server %s", member.guild.id)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    underlying = getattr(error, "original", error)
    if isinstance(underlying, ValueError):
        message = str(underlying)
    elif isinstance(underlying, (app_commands.CheckFailure, discord.Forbidden)):
        message = "You or Kuro are missing the required permissions. Check role order too."
    else:
        message = "That command failed. Check Kuro's console for details."
        log.error("Command failed", exc_info=(type(underlying), underlying, underlying.__traceback__))
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Copy .env.example to .env and set DISCORD_TOKEN first.")
    bot.run(token, log_handler=None)
