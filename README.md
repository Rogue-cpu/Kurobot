# Kuro Bot

A standalone Python Discord bot. Nothing in Synapse is required.

## First version

- `/ping`: connection check.
- `/deathnote member delay reason`: select a real member and schedule their ban, e.g. `2h`. Names are resolved through Discord's member picker.
- `/deathnote_cancel number`: cancel a pending ban in this server.
- `/deathnote_list`: recent jobs with pending, completed, cancelled, or failed results.
- `/roles_setup sticky autorole`: configure a join role and restore ordinary roles after someone leaves and rejoins. Omit the join role to turn it off.

Commands check moderator permissions and role order. Deathnote checks authority again when the timer expires. Join/sticky roles exclude moderation and administrative roles. Sticky roles begin recording departures after being enabled.

SQLite stores settings, remembered role IDs, and scheduled bans under `data/`. Keep this directory when moving the bot. The bot checks due bans every 15 seconds; overdue jobs run after reconnecting. Discord outages or permission failures mark jobs failed for moderator review. A process crash between Discord accepting a ban and the local completion save can cause that ban to be attempted again on restart.

## Run on Windows

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application named Kuro and open its Bot settings.
2. Enable **Server Members Intent**. Message Content Intent is not needed for this version.
3. Create an install link with `bot` and `applications.commands` scopes. Give Kuro **Ban Members** and **Manage Roles**; place its role above the roles/members it will manage.
4. Copy `.env.example` to `.env`. Put the bot token in that local file. Set `DISCORD_GUILD_ID` to your test server ID for immediate command registration. Do not send the token through chat.

```powershell
cd 'D:\kuro bot'
# The local virtual environment is already created during development.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env # only on first setup; then edit .env
.\.venv\Scripts\python.exe bot.py
```

Leave the process running for timers and join events. Global registration (without a test server ID) can take time to appear in Discord.

## Check locally

```powershell
.\.venv\Scripts\python.exe -m unittest test_storage -v
```

Live acceptance: check `/ping`, schedule and cancel a Deathnote, execute a short scheduled ban against a consenting test account, then check automatic/sticky roles using an ordinary test role and account. These actions require a connected Discord application and test server.

## Still to build

Verification, shared ban lists and whitelist controls, server/channel recovery, cross-server chat, activity levels, and premium-style utilities are not implemented yet. Full deleted-message history cannot be reconstructed without previously capturing those messages; a Discord bot cannot see membership lists of arbitrary servers it has not joined.
