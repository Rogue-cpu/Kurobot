# Kuro Bot

A standalone Discord bot for Kuro, hosted on Railway, separate from Synapse.

## Commands

| Feature | Commands |
| --- | --- |
| Connection and configuration | `/ping`, `/features_status` |
| Scheduled bans | `/deathnote`, `/deathnote_cancel`, `/deathnote_list` |
| Automatic and sticky roles | `/roles_setup` |
| Verification | `/verification_setup`, `/verification_disable` |
| Shared bans | `/network_create`, `/network_invite`, `/network_join`, `/network_leave`, `/globalban_add`, `/globalban_remove`, `/globalban_list`, `/whitelist` |
| Name-based Deathnote channel | `/deathnote_channel` |
| Cross-server chat | `/chat_invite`, `/chat_join`, `/chat_leave` |
| Channel recovery and backups | `/recovery_setup`, `/recovery_retry`, `/backup_export` |

All new features start off. Managers configure features; moderators need Ban Members for ban commands. Only a server's owner can create, join, invite, or leave a shared-ban network.

### Verification

`/verification_setup channel:#verify role:@Member min_account_days:7` posts a persistent button. Pressing Verify opens a random arithmetic challenge that expires after three minutes. A correct answer grants the configured role; challenges have a 30-second cooldown. Configure your channels so the verified role unlocks access. Kuro does not rewrite your server's permissions.

The role must be an ordinary role below both the configuring manager's and Kuro's highest roles. It cannot also be the join autorole, and sticky roles do not restore it. Verification is a simple challenge with optional account-age checking; it does not perform Double Counter-style device, IP, or alternate-account detection.

### Deathnote channel

`/deathnote_channel channel:#deathnote enabled:true` enables this syntax:

```text
username or display name or @member | 2h | ban reason
```

Kuro offers matching members, including similar names, in a selection menu. The moderator must select the intended member to schedule the ban. Duplicate names are never silently resolved. The menu expires after 90 seconds; only its original author can confirm it. The existing `/deathnote` command still works directly.

Timers and settings survive restarts when the database is on persistent storage. Due bans are checked every 15 seconds. Permissions and role order are checked again at execution. Discord failures mark jobs failed for review; overdue pending jobs run on reconnection. A crash after Discord accepts a ban but before saving completion can cause the same ban to be attempted again.

### Shared bans and local whitelist

The founding owner runs `/network_create`, then `/network_invite`. Another server's owner uses the single-use, one-hour code with `/network_join`. Joining applies existing bans and subscribes that server to future bans. Kuro needs Ban Members in each server.

Moderators in the founding server manage the network list using Discord user IDs. `/globalban_add` reports each connected server's result; `/globalban_list` shows the local enforcement result. `/whitelist user_id:... enabled:true` exempts that account from shared bans in your server. Owners, Kuro itself, and members above Kuro's role authority are protected. Network bans are also checked when members join.

Removing a shared entry, whitelisting someone, or leaving a network **does not undo existing Discord bans**. Lift those manually. Removing a whitelist exemption immediately reapplies any matching shared ban. If the founding server leaves, the network dissolves for all its member servers. Other servers can leave independently.

This is an invited network of servers using Kuro. It does not scrape membership from arbitrary servers or infer that someone belongs to a disliked server.

### Cross-server chat

A manager uses `/chat_invite channel:#shared-chat`; a manager in the other server uses `/chat_join channel:#shared-chat code:...`. Messages and attachment links are relayed between linked channels, labelled with server, channel, and author. Invitations expire after one hour and can only be used once. More servers can join using additional invitations.

Only channels visible to @everyone can participate. Relaying stops if a source or destination becomes private. Bot/webhook messages are ignored to prevent loops. Relays disable pings and embeds, forward at most one message per user per second, and truncate to Discord's 2,000-character message limit. Edits, deletions, threads, and replies are not synchronized. `/chat_leave` unlinks a channel; existing copies in other servers remain. Do not use a Deathnote channel for shared chat.

### Recovery and backups

`/recovery_setup enabled:true archive_messages:true` snapshots the server's categories and text, announcement, voice, stage, forum, and media channels. Kuro recreates deleted channels with saved names, positions, category placement, permission overwrites, and common channel settings. It also checks for missed deletions at startup and reconnection. It requires Manage Channels and Manage Roles, plus access to each channel. Missing permission roles block restoration instead of making a private channel public. Fix permissions/roles, then use `/recovery_retry` if needed.

Backups capture **new human messages in text/announcement channels after enabling**. They keep the latest 1,000 messages per channel, the latest observed edits, embed data, and attachment references. Attachments up to 8 MiB are also saved as files, within a total 256 MiB attachment budget. Larger files or downloads that fail retain links only; Discord attachment links may expire. Files belonging to messages evicted by the channel limit are pruned.

After channel recovery, Kuro replays the latest 50 saved messages as labelled archive copies and reuploads locally saved attachments. Full retained text/embed/reference data can be retrieved with `/backup_export`, which requires Manage Server plus access to the channel and its history. Saved attachment files are stored on the volume. Recovered verification channels get a new live button; shared-chat and Deathnote channel settings follow their replacement channels.

**Disable recovery before intentionally deleting channels.** Backups remain when recovery is disabled. This version does not recreate deleted roles, forum posts/threads, original message IDs/authorship, reactions, pins, invites, or every specialized channel setting. It does not punish attackers or operate without Kuro's permissions. Recovery restores deleted channels; it does not automatically roll back every edit to an existing channel. No server destruction or self-replication is implemented.

## Hosting and setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications), enable **Server Members Intent**. For message-based features, also enable **Message Content Intent**.
2. Invite Kuro with `bot` and `applications.commands`. Give it Ban Members, Manage Roles, Manage Channels, View Channel, Send Messages, Read Message History, and Attach Files where needed. Place its role above roles and members it will manage.
3. Set `DISCORD_TOKEN` in Railway's environment variables. Keep it out of Git and chat. Optional `DISCORD_GUILD_ID` registers commands immediately to a testing server; omit it for global commands.
4. Set `MESSAGE_CONTENT_ENABLED=true` only after enabling the corresponding Discord intent. Verification and shared bans work without it.
5. Attach a persistent Railway volume at `/app/data`. Railway's `RAILWAY_VOLUME_MOUNT_PATH` selects that directory automatically. The SQLite database and saved attachments both live there. A local `KURO_DATA_DIR` override is also supported.
6. Use `python bot.py` as the start command. Keep one replica running for timers and events.

For local development:

```powershell
cd 'D:\kuro bot'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env # first setup only; then edit .env
.\.venv\Scripts\python.exe bot.py
```

Do not overwrite an existing `.env`. Local credentials and `data/`, `.venv/`, and operator backups are excluded from Git.

## Validation

```powershell
.\.venv\Scripts\python.exe -m unittest test_features test_storage -q
```

Tests cover persistence and isolation, single-use invitations, whitelist and hierarchy enforcement, Deathnote parsing/matching, expired verification challenges, bounded message archives, recovery dependencies, and offline command registration. Live functional checks require two Discord servers and test accounts; deployment health alone does not establish that cross-server bans, relays, or deletion recovery work in your channel permissions.

Still outside this release: jail, leveling/activity perks, arbitrary-server membership enforcement, advanced alt detection, and full role/thread/history reconstruction.
