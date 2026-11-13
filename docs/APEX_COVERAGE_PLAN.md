# APEX | Complete Feature Replacement Standard

## Objective

Apex should eventually let a server owner uninstall redundant bots **because their required
functions work in Apex**, not because Apex happens to share their name or icon.

**Reference list:** \`docs/apex_feature_matrix.json\`, drawn from the owner's screenshots
of 32 bot accounts. Each source has an implementation status, existing Apex functions and
remaining unknown or missing work. This is a **living and currently incomplete** ledger,
not a claim of command-for-command parity.

### Coverage statuses

- **partial**: a relevant Apex module exists but important behaviors or premium options are missing.
- **missing**: no comparable native Apex module exists yet.
- **needs_research**: the name or installed bot's full function set is uncertain.
- **external**: a hosted listing network or ecosystem benefit cannot be recreated just
  by building Discord slash commands.

All 32 sources retain outstanding work or verification; no bot is ready for immediate
retirement merely because its name appears here.

### Current verified-from-code capabilities worth keeping

| Area | Current functions | Main gaps to evolve |
|---|---|---|
| Core moderation | Warnings, warning review, purge, scheduled bans, timed jail, opt-in shared bans | Full configurable AutoMod, timed moderation policies, case search, dashboard |
| Security | Vanity alert/quarantine logic, permission warnings, reversible manual lockdown, verification policy; anti-nuke and raid monitors on development branch | Real quarantine tests, allowlists, JoinGate, role tamper containment, anti-spam |
| Roles and community | Autoroles, sticky roles, verification buttons, selfrole picker, XP and role rewards | Advanced reaction/button roles, role grouping, temporary roles, welcome/leave, polls, giveaways |
| Backups | Saved channel structure/message refs, public threads, role reviews, owner recovery bundles, invited blueprints | Xenon-style scheduled server versions, diff previews, precise selective restore, emoji/sticker inventory |
| Support | **New development module**: native private support ticket opening/closing and owner panel | ModMail DM relay, application forms, ticket staff assignment, transcript workflows |
| Communication | Opt-in cross-server relay and staff controls | Custom commands, autoresponders, scheduled posts, RSS/YouTube/stream feeds |
| Media and creative | None of the specialized music, voice, quote, or proxy features fully available | Licensed playback/TTS, statistics, quote images, character proxies, emoji/import, embeds |

These rows describe checked-in implementations, not independently validated production
behavior. Tests and live Discord permissions determine actual readiness.

### Parity acceptance gate for every capability

1. **Specification**: Read the source bot's documentation and the current server's
   actual enabled commands/settings. Capture premium features used in this server.
2. **Deduplication**: If Apex already has the capability, link to the implementation.
   Improve that module in-place only when substantive features or safety guarantees are missing.
3. **Implementation**: Design a native Apex API/module with config, owner controls,
   permission checks, bounded storage, error handling and event logs.
4. **Automated proof**: Add unit and failure-path tests that cover permissions,
   unauthorized invocation, persistence, duplicates, limits and reversibility.
5. **Live proof**: Test on a disposable Discord server, not on an irreplaceable vanity
   code or a live staff workflow. Verify behavior and document any API limitations.
6. **Migration**: Confirm all workflows needed in the target server, backup source
   settings, disable competing automation, observe, and only then let its owner remove
   the redundant third-party bot.

A source bot is **replaced** only when its complete in-use capability checklist has
been met or the owner explicitly accepts documented omissions.

## Priority completion passes

**Pass 1: Administration parity.** Dyno and YAGPDB AutoMod, custom commands,
welcome/leave, action logs, timed messages, role menus and per-guild settings.

**Pass 2: Security parity.** Security Bot/Honeypot integration, invite tracking,
join checks, role change recovery, safe quarantine, tamper alerts and opt-in
anti-raid actions. Preserve Discord native AutoMod as an independent safeguard.

**Pass 3: Support parity.** Ticket Tool and ModMail complete casework, including
forms, permissions, assignment, reopen/close behavior and owner-approved exports.
Applications come after core ticket isolation is proven.

**Pass 4: Preservation parity.** Xenon versioned backups and server templates,
restore previews, settings/emoji/sticker coverage within Discord API limits,
automated verification and reversible rollback.

**Pass 5: Community parity.** Arcane premium-like level settings, giveaways,
polls, quote cards, roleplay proxy characters and game features after each
source-bot's actual command set is audited.

**Pass 6: Media/integration parity.** YouTube subscriptions, authorized Last.fm
statistics, TTS, music playback where licensing and available APIs permit,
AI helpers and other external feeds.

## External boundaries

- **DISBOARD, Discodus and DiscordServers:** A private Apex bot cannot replace
  the existing public traffic or network of those sites by implementing a bump
  command. Keep these integrations if traffic and directory visibility are important.
- **Third-party music/AI services:** Require their own licenses, APIs, token policies,
  privacy constraints and authorization. Do not assume full functionality is available.
- **Logo/brand assets:** Apex branding remains original. Competing bot icons are not
  part of Apex's official brand.
- **Source documentation:** Official docs help specify products, but each server
  may use only a small subset of their features. Do not silently claim all premium
  functions have been audited by reading one marketing page.

## Starting references (official public documentation)

- [Dyno modules](https://docs.dyno.gg/en/modules)
- [YAGPDB documentation](https://github.com/botlabs-gg/yagpdb-docs)
- [Xenon backup and template docs](https://xenon.bot/docs)
- [InviteLogger command documentation](https://docs.invitelogger.me/general-topics/commands/invites)

Other bot rows marked **needs_research** must be validated against their current
official documentation or an owner-supplied command export before exact parity targets
are locked in.

## Status tracking

Update the JSON matrix every time a feature moves from planned to implemented,
and then to disposable-server verified. Continue to classify source bots as
**partial** until their in-use settings and all critical commands are covered.
\`test_apex_matrix.py\` enforces the complete screenshot inventory and prevents
premature claims that every bot is fully replaced.
