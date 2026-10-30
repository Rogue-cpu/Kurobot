"""Application-owner backup controls. Full bot archives are never posted to Discord."""
import asyncio
import logging
import re
import secrets
import time
from datetime import datetime,timezone

import discord
from discord import app_commands
from discord.ext import commands,tasks

from recovery_backup import create_bundle,verify_bundle

log=logging.getLogger("kuro.backups")
BACKUP_NAME=re.compile(r"kuro-backup-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\.zip\Z")


class Backups(commands.Cog):
    backups=app_commands.Group(name="backups",description="Application-owner recovery backups",guild_only=True)

    def __init__(self,bot,store):
        self.bot,self.store=bot,store
        self.lock=asyncio.Lock()

    async def cog_load(self):
        self.timer.start()

    async def cog_unload(self):
        self.timer.cancel()

    async def owner(self,interaction):
        if not await self.bot.is_owner(interaction.user):
            raise ValueError("Only Kuro's application owner can manage full-bot backups, because they include records from all connected servers.")

    async def create(self):
        if self.lock.locked():
            raise ValueError("A backup is already running.")
        async with self.lock:
            now=int(time.time())
            self.store.set_option(0,"backup_attempt",now)
            stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name=f"kuro-backup-{stamp}-{secrets.token_hex(4)}.zip"
            directory=self.bot.data_dir/"backups"
            directory.mkdir(parents=True,exist_ok=True)
            try:
                manifest=await asyncio.to_thread(create_bundle,self.bot.data_dir,directory/name)
                record={"name":name,"created":now,"bytes":(directory/name).stat().st_size,"missing":len(manifest["missing_attachments"])}
                records=self.store.option(0,"backup_records",[])+[record]
                policy=self.store.option(0,"backup_policy",{})
                keep=policy.get("keep",3)
                retained=records[-keep:]
                # Save the new record before pruning previous generated bundles.
                self.store.set_option(0,"backup_records",retained)
                self.store.set_option(0,"backup_error",None)
                for old in records[:-keep]:
                    if BACKUP_NAME.fullmatch(old["name"]):
                        old_path=directory/old["name"]
                        if old_path.is_file() and not old_path.is_symlink():
                            old_path.unlink()
                return record
            except Exception as exc:
                self.store.set_option(0,"backup_error",type(exc).__name__)
                raise

    @backups.command(name="setup",description="Enable or disable scheduled full-bot backups on the existing volume.")
    async def setup(self,interaction: discord.Interaction,enabled: bool,interval_hours: app_commands.Range[int,6,168]=24,keep: app_commands.Range[int,1,5]=3):
        await self.owner(interaction)
        self.store.set_option(0,"backup_policy",{"enabled":enabled,"hours":interval_hours,"keep":keep})
        await interaction.response.send_message(f"Scheduled backups {'enabled' if enabled else 'disabled'}; interval {interval_hours} hours, keep {keep}. Bundles stay on Kuro's existing volume. Keep a downloaded copy elsewhere for volume-loss recovery, and allow space for multiple archives.",ephemeral=True)

    @backups.command(name="create",description="Create and verify a recovery bundle without posting private data to Discord.")
    async def create_command(self,interaction: discord.Interaction):
        await self.owner(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            record=await self.create()
            await interaction.followup.send(f"Saved and verified `{record['name']}` in the volume's `backups` folder. Size: {record['bytes']:,} bytes. Missing attachment files: {record['missing']}. Download it privately using Railway's volume file tools.",ephemeral=True)
        except Exception as exc:
            log.warning("Manual backup failed: %s",type(exc).__name__)
            await interaction.followup.send("Backup did not finish. Check disk space and Kuro's backup status; no archive has been posted to Discord.",ephemeral=True)

    @backups.command(name="status",description="Show the backup schedule and the last saved archive records.")
    async def status(self,interaction: discord.Interaction):
        await self.owner(interaction)
        policy=self.store.option(0,"backup_policy",{})
        records=self.store.option(0,"backup_records",[])
        lines=[f"Schedule: {'on' if policy.get('enabled') else 'off'}; every {policy.get('hours',24)} hours; keep {policy.get('keep',3)}."]
        lines += [f"{record['name']}: {record['bytes']:,} bytes; missing attachment files {record['missing']}" for record in records]
        error=self.store.option(0,"backup_error")
        if error:
            lines.append("Last backup error: "+error)
        if not records:
            lines.append("No automatic or command-created bundles recorded yet.")
        await interaction.response.send_message("\n".join(lines)[:1900],ephemeral=True)

    @backups.command(name="verify",description="Recheck one recorded recovery archive's checksums and database.")
    async def verify(self,interaction: discord.Interaction,name: str):
        await self.owner(interaction)
        known={record["name"] for record in self.store.option(0,"backup_records",[])}
        if name not in known or not BACKUP_NAME.fullmatch(name):
            raise ValueError("Choose a recorded filename shown by /backups status.")
        await interaction.response.defer(ephemeral=True)
        try:
            manifest=await asyncio.to_thread(verify_bundle,self.bot.data_dir/"backups"/name)
            await interaction.followup.send(f"Checksums and database integrity passed. Files in bundle: {len(manifest['files'])}; missing attachment files recorded: {len(manifest['missing_attachments'])}.",ephemeral=True)
        except Exception as exc:
            log.warning("Backup verification failed: %s",type(exc).__name__)
            await interaction.followup.send("Archive verification failed. Keep the current data and check another backup before restoring.",ephemeral=True)

    @tasks.loop(minutes=5)
    async def timer(self):
        policy=self.store.option(0,"backup_policy",{})
        if not policy.get("enabled"):
            return
        records=self.store.option(0,"backup_records",[])
        now=int(time.time())
        if records and now-records[-1]["created"] < policy.get("hours",24)*3600:
            return
        if now-self.store.option(0,"backup_attempt",0)<3600:
            return
        try:
            await self.create()
            log.info("Scheduled recovery bundle created and verified")
        except Exception as exc:
            log.warning("Scheduled backup failed: %s",type(exc).__name__)

    @timer.before_loop
    async def before_timer(self):
        await self.bot.wait_until_ready()
