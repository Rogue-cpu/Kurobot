# Kuro Bot Beginner Guide

Kuro helps you run a Discord server with verification, moderation, activity rewards, backups, and linked-server features. This guide starts with everyday setup. You can leave features you do not need turned off.

## Start here

Type `/` in a Discord channel, choose Kuro, and select a command. Discord shows boxes for the information it needs. Choose people, roles, and channels from those boxes. You do not need to write code.

Commands below are examples. Replace `#verify`, `@Member`, and other example names with your own channels and roles. `true` means on. `false` means off. Most setup replies are visible only to you.

Run `/ping` to check that Kuro is awake. Run `/features_status` to see which features you have configured.

Use `/guide` in Discord whenever you want a link back to this guide.

## Set up roles and permissions first

Open your server menu, then **Server Settings > Roles**. Create ordinary roles named **Member**, **Jailed**, and optionally **Regular**. Keep moderation permissions off for these roles.

Move Kuro's role above the roles it will give people and above the members it will moderate. Kuro needs **Manage Roles**, **Moderate Members**, **Ban Members**, and **Manage Channels** for the related features. It also needs access to the channels it uses, including **View Channel**, **Send Messages**, **Read Message History**, and **Attach Files**. Message cleanup needs **Manage Messages**. Thread recovery needs the relevant thread permissions.

You do not need to give Kuro Administrator just to get started. Staff roles should stay separate from ordinary member, activity, optional, verification, and jail roles.

## Your first setup

1. Create a channel named **verify** and choose a normal chat channel for members.
2. Run `/verification_setup channel:#verify role:@Member min_account_days:7`.
3. Open your member chat channel's **Edit Channel > Permissions**. Turn **View Channel** off for `@everyone` and on for `Member`. Keep the verify channel visible to newcomers. Repeat this for the member channels you want to protect.
4. Run `/jail setup role:@Jailed`.
5. Run `/levels setup enabled:true` if you want activity points.
6. Run `/recovery_setup enabled:true archive_messages:true` to save channel layout and new messages.
7. Run `/recovery_status`. Check that your categories and channels were saved.

Try verification with an ordinary test account. A server owner or administrator can see channels that ordinary members cannot, so an owner account is not a useful test of the newcomer experience.

## Verification and account review

Newcomers press **Verify** in your verify channel and answer a short arithmetic question. A correct answer grants the Member role if the account meets your settings. `min_account_days:7` means the account must be at least seven days old.

For extra moderator review, run `/verification_policy setup enabled:true require_avatar:true join_burst:10`. Accounts without a custom avatar, accounts below your minimum age, membership-rule flags, or a burst of ten joins in five minutes can be sent for review. Set `join_burst:0` to turn that particular check off.

Use `/verification_policy reviews` to see the queue. Use `/verification_policy decide member:@Someone approve:true` to approve a person you have reviewed, or `approve:false` to decline. Declining verification does not ban the person. Resolve an active jail or shared ban before approving verification.

These checks help you review suspicious joins. They do not prove that two accounts belong to the same person. Kuro cannot read someone's IP address or device identity from Discord.

Use `/verification_disable` to stop verification. Existing member roles remain.

## Jail and everyday moderation

Use `/jail add member:@Someone duration:2h reason:Repeated spam` to apply a timed jail. Kuro uses Discord's timeout to stop chatting and voice participation, plus the Jailed role as a label. Jail is not a separate room where the timed-out person can chat.

Durations use `s` for seconds, `m` for minutes, `h` for hours, and `d` for days. A jail can last up to 28 days. It expires automatically. `/jail release member:@Someone` ends Kuro's jail early. A timeout changed separately by another moderator is left intact. `/jail list` shows active jails.

Use `/tools warn member:@Someone reason:Please stop spamming` to save a warning. `/tools warnings member:@Someone` shows recent warnings. Warnings do not send a private message or automatically punish someone.

Use `/tools purge count:10` in a channel to delete its ten latest messages. Existing Kuro backups are retained. Try this only in a disposable test channel while learning.

## Activity levels and optional roles

`/levels setup enabled:true` starts activity points. By default, a useful chat message earns 15 XP at most once a minute. Very short messages and immediate repetitions do not earn XP. Bot messages do not earn XP.

Members use `/levels rank` and `/levels leaderboard`. Level 1 needs 100 XP, level 2 needs 400 XP, and level 3 needs 900 XP. Each level needs more activity than the last.

Use `/levels reward level:5 role:@Regular` to give a normal role at level 5. Members receive eligible rewards on their next message that earns XP. Rewards accumulate. `/levels remove_reward level:5` stops future grants; it does not take away roles already earned.

For roles people can choose themselves, use `/selfrole setup role:@Art enabled:true`. Members can then use `/selfrole list` and `/selfrole pick role:@Art enabled:true`. Set `enabled:false` in pick to remove that role from yourself. Kuro checks the role's permissions again when someone picks it.

## Deathnote commands and channel

Use `/deathnote member:@Someone delay:2h reason:Your reason` to schedule a ban. `/deathnote_list` shows recent schedules. `/deathnote_cancel number:12` cancels pending Deathnote number 12.

To use a dedicated channel, run `/deathnote_channel channel:#deathnote enabled:true`. Moderators can write:

```text
Username or display name | 2h | Your ban reason
```

Kuro offers matching members. The moderator must choose the intended person to confirm the ban. Similar names are suggestions, not automatic bans. The selection expires after 90 seconds.

Scheduled bans survive restarts. Kuro checks them about every 15 seconds while running and checks permissions again before banning.

## A server that heals after channel deletion

Run `/recovery_setup enabled:true archive_messages:true`. Kuro saves the current channel layout, then automatically saves channels as they are created or moved. Each channel remembers its category, position, name, permission overwrites, and supported settings.

If someone deletes a channel, Kuro puts its saved copy in the recovery queue. If a category is deleted too, Kuro rebuilds the category first and then restores its channels under it. Surviving channels that lost their category are moved back into the recreated category.

Healing is gradual: Kuro attempts roughly one channel recovery step every 15 seconds. A large deletion can take a while. Discord rate limits, missing permissions, and role restoration can make it take longer. Saved layout and queued work live on Railway's persistent volume, so restarting Kuro does not erase them.

Use `/recovery_status` to see missing channels grouped by category. If a step fails, Kuro keeps the failure and retries later with a growing delay. Fix the permission problem and use `/recovery_retry` to try the next step sooner.

**Before intentionally deleting channels**, run `/recovery_setup enabled:false`. Delete the channels you really want gone. Turn recovery back on afterward to save a fresh channel layout.

New human messages are backed up after message backups are enabled. By default, Kuro keeps 1,000 messages per channel and replays the latest 50 after recovery. Replayed messages are labelled copies from Kuro. Original message IDs, authorship, reactions, and links cannot be recreated.

## Role recovery forum posts and older history

The server owner can enable extra recovery:

```text
/recovery_plus setup roles:true threads:true messages_per_channel:5000 replay_count:50 include_bots:false
```

This saves ordinary and staff role definitions and visible active public threads/forum posts. Missing roles are recreated before missing channels. Eligible ordinary role assignments are restored gradually. Staff, verification, and jail assignments require review rather than automatically giving sensitive access back. Kuro can only recreate role permissions within its own authority.

The owner can use `/recovery_plus role_review role:@RecoveredRole` to download the former member IDs for a recovered sensitive role. Review them before assigning that role manually.

Public forum posts and public threads can be recreated with saved replies. Private threads are not copied or recovered. Enable these features before deleting anything you expect Kuro to recover. Disable the relevant extra recovery option before intentionally deleting roles or threads.

To capture existing messages, use `/recovery_plus history channel:#general limit:100 include_threads:true`. This captures up to 100 existing messages per channel/thread and up to 100 accessible archived public threads. You can choose up to 1,000 messages per capture. New messages continue filling the configured retention limit afterward.

Use `/backup_export channel:#general` to download retained text and attachment references. Saved attachments up to 8 MiB are kept within a total 256 MiB attachment budget. Bigger files or failed downloads keep links only, and those links may expire. Kuro's own archive replay messages are never backed up again.

Keep backups of the Railway volume as well. A persistent volume survives ordinary redeployment; it is not a separate off-site backup.

## Shared bans across servers

First invite the same Kuro bot into both servers. Each server's owner chooses whether to participate.

1. In the founding server, its owner runs `/network_create name:Friends`.
2. That owner runs `/network_invite` and privately gives the code to the other owner.
3. The other owner runs `/network_join code:THE_CODE` in their server.
4. A founding-server moderator uses `/globalban_add user_id:123456789012345678 reason:Your reason`.

The ban is applied to participating servers where Kuro has authority. The command reports results. `/globalban_list` shows the list and local enforcement results.

Use `/whitelist user_id:123456789012345678 enabled:true` to exempt someone in your server. This also exempts them from membership-watch enforcement. It does not remove a Discord ban that already exists.

`/globalban_remove` removes the shared list entry. `/network_leave` stops future shared bans for that server. Existing Discord bans must be lifted manually. If the founding server leaves, its network dissolves.

To copy a Discord user ID, enable **User Settings > Advanced > Developer Mode**, then right-click or open the person's menu and choose **Copy User ID**.

## Membership watch rules

These rules check whether someone belongs to another server that has agreed to be a membership source. They cannot scan a server Kuro has not joined or bypass Discord's access rules.

The source server's owner runs `/watch invite` and shares the code with your server's owner. Your owner runs `/watch add code:THE_CODE action:flag reason:Review overlapping membership`.

Start with **flag** while learning. The available actions are flag, one-day jail, or ban. Jail needs `/jail setup` first. Automatic actions are queued and membership is checked again before enforcement. An owner change stops automatic enforcement until the new owner approves a replacement rule.

New joins are checked. `/watch scan source_server_id:THE_ID limit:1000` checks existing overlaps. A scan that reaches its chosen limit may be partial. `/watch list` shows rules and recent results. `/watch resolve member:@Someone` records a local clear decision without undoing a punishment. That decision survives repeat checks until the source rule is renewed. The whitelist exempts a person across all source rules in your server.

The target owner can stop a rule with `/watch remove`. The source owner can withdraw permission with `/watch revoke target_server_id:THE_ID`. Previous bans and jails remain until explicitly lifted.

## Chat between servers

Use a dedicated public channel in each server. Do not link private staff channels or your Deathnote channel.

In the first server, a manager runs `/chat_invite channel:#shared-chat`. A manager in the second server runs `/chat_join channel:#shared-chat code:THE_CODE`. Invitations last one hour and work once.

Human messages and attachment links are relayed with server, channel, and author labels. Pings and automatic embeds are disabled. Very fast messages are limited, and long messages may be shortened. Edits and deletions are not synchronized.

Use `/chat_leave channel:#shared-chat` to unlink a channel. Old copies in other servers remain. Relaying stops if a linked channel becomes private.

## Copy a server layout

Create the destination server yourself and invite Kuro into it. Blueprints copy structure into a server you already own; they do not create Discord accounts or bot clones.

The source owner runs `/blueprint create` and shares the code privately with the destination owner. In the destination, its owner runs `/blueprint import code:THE_CODE`, reads the summary, and presses **Add this blueprint**.

Kuro adds saved role definitions, categories, and channels gradually. Existing destination channels stay. Members, message history, bot credentials, and individual-member permission rules are not copied. Incompatible permissions stop the job for review rather than silently opening private channels.

Use `/blueprint status` for progress. `/blueprint cancel` stops pending steps; a step already underway may finish, and existing copies remain. If a job fails or a creation step is interrupted, inspect the partial result before starting another import. Do not repeatedly submit the same copy.

## Owner alerts lockdown and small conveniences

The owner can run `/protection setup channel:#staff-log enabled:true` to receive alerts about ownership changes or another bot leaving the server. A removed Kuro cannot keep operating in that server.

`/protection lock` applies saved restrictions to ordinary chat and voice permissions. Administrators can still act. `/protection unlock` restores Kuro's permission changes while leaving unrelated later edits in place. Failed changes are reported; fix Kuro's permissions and try again if needed. These commands require the current owner.

Kuro does not include a server-destruction time bomb, owner-transfer retaliation, or a bot-removal destruction trigger.

Use `/tools sticky channel:#rules enabled:true text:Please read the rules` for a reminder that returns after human activity, at most once a minute. Set enabled to false to stop it.

Use `/tools forum` to set forum guidelines and the default archive duration from its dropdown. Archiving inactive forum posts does not delete them.

## If something does not work

- **Kuro cannot give a role**: check Manage Roles and move Kuro above that role. Use an ordinary role without staff permissions.
- **Kuro cannot moderate someone**: check the relevant moderation permission and role order. Owners and administrators are protected from jail.
- **Messages are not being read**: enable Message Content Intent in the Discord Developer Portal and set `MESSAGE_CONTENT_ENABLED=true` on Railway. Use `/features_status` to check message access.
- **Channels do not recover**: check `/recovery_status`. Recovery must have been enabled before deletion, Kuro must still be in the server, and it needs permission to recreate the saved layout.
- **Changes disappear after restarting**: check that the service still mounts its persistent volume and uses the same database.
- **A feature seems inactive**: run `/features_status`. Most features stay off until you configure them.

Start with verification, jail, and channel recovery. Add cross-server features after the everyday setup works for your members.
