"""Functional Apex command hub: one gateway into modular Discord features.

No third-party bot catalog or copied branding. Discord select menus organise
features; high-impact administrative actions remain in explicit slash commands.
"""
import discord
from discord import app_commands
from discord.ext import commands

from features import NO_MENTIONS, manager, require_owner

SECTIONS = {
    "overview": ("Apex Control Center", "🏛️", "One bot. All systems."),
    "security": ("Security", "🛡️", "Vanity protection, anti-nuke and anti-raid."),
    "moderation": ("Moderation", "⚖️", "Warn, ban, timeout and case management."),
    "tickets": ("Support & Tickets", "🎫", "Private staff assistance and requests."),
    "community": ("Community", "👥", "Verification, roles, XP and events."),
    "media": ("Music & Media", "🎵", "Music, video, voice and publishing."),
    "backups": ("Backup & Recovery", "🗄️", "Server snapshots, templates and restoration."),
    "utilities": ("Utilities & AI", "🧩", "Embeds, quotes, emoji and intelligent tools."),
    "automation": ("Automation", "⚙️", "Messages, feeds, cleanup and custom rules."),
}

# label, state, description, commands, built-in safe button action
FEATURES = {
    "security": {
        "vanity": ("Vanity Guard", "implemented", "Monitor protected vanity code, review status and optional containment.", "/vanity status • /vanity risk • /vanity response", "vanity"),
        "antinuke": ("Anti-Nuke", "implemented", "Audit-log detection and owner alerts for destructive behavior.", "/antinuke setup • /antinuke status • /antinuke test", "antinuke"),
        "raid": ("Raid Guard", "implemented", "Join-burst warnings and recently created account signals.", "/raid setup • /raid status • /raid test", "raid"),
        "lockdown": ("Manual Lockdown", "implemented", "Owner-confirmed reversible server lockdown.", "/protection setup • /protection lock • /protection unlock", None),
        "advanced": ("Advanced Containment", "planned", "Expanded anti-nuke containment with verified owner exemptions and rollback.", "Planned, not deployed", None),
    },
    "moderation": {
        "warnings": ("Warnings & Purge", "implemented", "Staff warnings, warning history and channel cleanup.", "/tools warn • /tools warnings • /tools purge", None),
        "deathnote": ("Scheduled Bans", "implemented", "Permission-checked delayed ban jobs and cancellation.", "/deathnote • /deathnote_cancel • /deathnote_list", None),
        "jail": ("Timed Jail", "implemented", "Temporarily restrict members with configured roles.", "/jail setup • /jail add • /jail release", None),
        "globalban": ("Ban Network", "implemented", "Opt-in connected-server ban membership and exemptions.", "/network_create • /globalban_add • /whitelist", None),
        "automod": ("Advanced AutoMod", "planned", "Automated spam, links, flooding and content rules.", "Planned, not deployed", None),
    },
    "tickets": {
        "open": ("Open Support Ticket", "implemented", "Create your own private staff-support channel.", "Also available from a public ticket panel", "open_ticket"),
        "setup": ("Ticket Setup", "implemented", "Owner configuration for category, staff role and log channel.", "/tickets setup • /tickets panel • /tickets status", "tickets"),
        "modmail": ("ModMail", "planned", "Private user-to-staff relay with case history.", "Planned, not deployed", None),
        "applications": ("Applications", "planned", "Application forms, review queues and staff decisions.", "Planned, not deployed", None),
    },
    "community": {
        "verification": ("Verification", "implemented", "Persistent member verification panel with challenge.", "/verification_setup • /verification_disable", None),
        "roles": ("Self-Roles & Sticky Roles", "implemented", "Role menu and server rejoin role restoration.", "/roles_setup • /selfrole setup • /selfrole pick", None),
        "levels": ("Levels & XP", "implemented", "XP leaderboard and role milestone rewards.", "/levels setup • /levels rank • /levels leaderboard", None),
        "giveaways": ("Giveaways & Polls", "planned", "Auditable polls and giveaways.", "Planned, not deployed", None),
        "roleplay": ("Roleplay Proxy", "planned", "User-authorized character identities and proxy messaging.", "Planned, not deployed", None),
    },
    "media": {
        "audio": ("Music Playback", "planned", "Licensed/authorized music and supported audio controls.", "Planned, not deployed", None),
        "fm": ("Music Statistics", "planned", "Optional last.fm account linking with consent.", "Planned, not deployed", None),
        "tts": ("Text to Speech", "planned", "Staff-controlled speech playback in voice channels.", "Planned, not deployed", None),
        "video": ("YouTube Notifications", "planned", "Opt-in verified channel updates.", "Planned, not deployed", None),
    },
    "backups": {
        "backup": ("Recovery Backups", "implemented", "Application-owner snapshots and recovery verification.", "/backups setup • /backups create • /backups verify", "backups"),
        "channel": ("Channel Recovery", "implemented", "Saved server structure and selective channel restoration.", "/recovery_setup • /recovery_status • /recovery_retry", None),
        "blueprint": ("Blueprints", "implemented", "Consent-based server structure templates.", "/blueprint create • /blueprint import", None),
        "genebank": ("KuroGene Template Library", "planned", "Versioned template catalog with safe import previews.", "Planned, not deployed", None),
    },
    "utilities": {
        "embeds": ("Embed Builder", "planned", "Custom embed composition and publishing.", "Planned, not deployed", None),
        "emoji": ("Emoji Tools", "planned", "Authorized emoji and sticker utilities.", "Planned, not deployed", None),
        "quotes": ("Quote Images", "planned", "Render user-authorized quoted messages as images.", "Planned, not deployed", None),
        "ai": ("Apex AI Assistance", "planned", "Human-controlled Nikra AI tools with privacy boundaries.", "Planned, not deployed", None),
        "bible": ("Reference Tools", "planned", "Bible references using permitted translations.", "Planned, not deployed", None),
    },
    "automation": {
        "chat": ("Cross-Server Chat", "implemented", "Opt-in links between public server channels.", "/chat_invite • /chat_join • /chat_leave", None),
        "welcome": ("Welcome & Goodbye", "planned", "Configurable user join and leave messages.", "Planned, not deployed", None),
        "replies": ("Custom Commands", "planned", "Safe owner-defined responses and rules.", "Planned, not deployed", None),
        "cleanup": ("Auto Delete", "planned", "Scheduled retention and cleanup, opt-in per channel.", "Planned, not deployed", None),
        "feeds": ("Notifications", "planned", "Authorized RSS/video/stream notification feeds.", "Planned, not deployed", None),
        "discovery": ("Server Discovery", "external", "Directory reach remains on external listings such as DISBOARD.", "External service", None),
    },
}

STATUS = {
    "implemented": "✅ Implemented (validate on your server)",
    "planned": "⚪ Not yet implemented",
    "external": "🔗 External service",
}


def feature_embed(section, feature=None, bot=None):
    label, emoji, tagline = SECTIONS[section]
    embed = discord.Embed(
        title=emoji + " " + label,
        description=tagline,
        color=discord.Color.from_rgb(187, 27, 50),
    )
    if bot is not None and bot.user is not None:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    if section == "overview":
        embed.add_field(
            name="Choose your system",
            value="Use the selector to navigate Security, Moderation, Support, "
            "Community, Media, Backups, Utilities and Automation.",
            inline=False,
        )
        embed.add_field(
            name="Integrated features",
            value="Implemented functions remain available through their slash "
            "commands. Safe, built-in actions also appear as buttons below.",
            inline=False,
        )
        embed.add_field(
            name="Development rule",
            value="Planned features are labeled clearly. Apex does not pretend "
            "that a competitor's commands already work inside it.",
            inline=False,
        )
    else:
        features = FEATURES[section]
        if feature is not None:
            name, state, description, commands, action = features[feature]
            embed.title = emoji + " " + name
            embed.description = description
            embed.add_field(name="Availability", value=STATUS[state], inline=False)
            embed.add_field(name="Commands", value=commands, inline=False)
            if action:
                embed.add_field(
                    name="Quick action",
                    value="Use **Run / View** below to access this feature directly.",
                    inline=False,
                )
        else:
            for name, state, description, commands, action in features.values():
                embed.add_field(
                    name=name + " | " + ("✅" if state == "implemented" else "🔗" if state == "external" else "⚪"),
                    value=description + "\n" + commands,
                    inline=False,
                )
    embed.set_footer(text="APEX • One bot, modular features • Owner panel")
    return embed


class SystemSelect(discord.ui.Select):
    def __init__(self, current):
        super().__init__(
            placeholder="Select an Apex system…",
            options=[
                discord.SelectOption(
                    label=value[0], value=key, emoji=value[1], default=key == current,
                    description=value[2][:100],
                )
                for key, value in SECTIONS.items()
            ],
            row=0,
        )

    async def callback(self, interaction):
        self.view.section = self.values[0]
        self.view.feature = None
        self.view.rebuild()
        await interaction.response.edit_message(
            embed=feature_embed(self.view.section, bot=interaction.client),
            view=self.view,
        )


class FeatureSelect(discord.ui.Select):
    def __init__(self, section, selected=None):
        super().__init__(
            placeholder="Open a feature…",
            options=[
                discord.SelectOption(
                    label=item[0][:100],
                    value=key,
                    description=STATUS[item[1]][2:][:100],
                    default=key == selected,
                )
                for key, item in FEATURES[section].items()
            ],
            row=1,
        )

    async def callback(self, interaction):
        self.view.feature = self.values[0]
        self.view.rebuild()
        await interaction.response.edit_message(
            embed=feature_embed(
                self.view.section, self.view.feature, bot=interaction.client
            ),
            view=self.view,
        )


class RunButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Run / View", emoji="▶️", style=discord.ButtonStyle.primary, row=2,
        )

    async def callback(self, interaction):
        view = self.view
        feature = FEATURES[view.section][view.feature]
        action = feature[4]
        cog = interaction.client.get_cog("ApexHub")
        if cog is None:
            await interaction.response.send_message(
                "Apex control features are currently unavailable.", ephemeral=True,
            )
            return
        await cog.run_quick_action(interaction, action)


class HomeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Home", emoji="🏠", style=discord.ButtonStyle.secondary, row=2,
        )

    async def callback(self, interaction):
        self.view.section = "overview"
        self.view.feature = None
        self.view.rebuild()
        await interaction.response.edit_message(
            embed=feature_embed("overview", bot=interaction.client),
            view=self.view,
        )


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close", style=discord.ButtonStyle.danger, row=2,
        )

    async def callback(self, interaction):
        self.view.stop()
        await interaction.response.edit_message(
            content="Apex control center closed.", embed=None, view=None,
        )


class ApexHubView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.section = "overview"
        self.feature = None
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(SystemSelect(self.section))
        if self.section in FEATURES:
            self.add_item(FeatureSelect(self.section, self.feature))
        if (self.feature is not None and self.section in FEATURES
                and FEATURES[self.section][self.feature][4]):
            self.add_item(RunButton())
        self.add_item(HomeButton())
        self.add_item(CloseButton())

    async def interaction_check(self, interaction):
        ok = (
            interaction.user.id == self.owner_id
            and interaction.guild_id == self.guild_id
            and interaction.user.id == interaction.guild.owner_id
        )
        if not ok:
            await interaction.response.send_message(
                "This panel is restricted to the server owner who opened it.",
                ephemeral=True,
            )
        return ok


class ApexHub(commands.Cog):
    def __init__(self, bot, store):
        self.bot = bot
        self.store = store

    @app_commands.command(
        name="hub", description="Open Apex's unified interactive feature control center.",
    )
    @manager
    async def hub(self, interaction: discord.Interaction):
        require_owner(interaction)
        await interaction.response.send_message(
            embed=feature_embed("overview", bot=self.bot),
            view=ApexHubView(interaction.user.id, interaction.guild_id),
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    async def run_quick_action(self, interaction, action):
        require_owner(interaction)
        guild = interaction.guild
        if action == "open_ticket":
            tickets = self.bot.get_cog("Tickets")
            if tickets is None:
                raise ValueError("Native ticket system is not loaded.")
            await tickets.open_ticket(interaction)
            return
        if action == "vanity":
            guard = self.bot.get_cog("VanityGuard")
            if guard is None:
                raise ValueError("Vanity guard is unavailable.")
            cfg = guard.config(guild.id)
            if not cfg.get("enabled"):
                message = "Vanity Guard is off. Set it up with /vanity setup."
            else:
                live = await guard.current_code(guild)
                message = (
                    "Protected: discord.gg/" + cfg["code"]
                    + "\nLive: " + ("discord.gg/" + live if live else "(unset)")
                    + "\nResult: " + ("MATCH" if live == cfg["code"] else "MISMATCH")
                    + "\nResponse mode: " + cfg.get("response", "alert")
                )
        elif action == "antinuke":
            cfg = self.store.option(guild.id, "antinuke_config", {}) or {}
            message = "Anti-Nuke alerts: " + ("enabled" if cfg.get("enabled") else "disabled")
            if cfg.get("enabled"):
                message += "\nThreshold: " + str(cfg["threshold"]) + " events in " + str(cfg["window_seconds"]) + "s"
            message += "\nConfigure: /antinuke setup"
        elif action == "raid":
            cfg = self.store.option(guild.id, "raid_alert_config", {}) or {}
            message = "Raid warnings: " + ("enabled" if cfg.get("enabled") else "disabled")
            if cfg.get("enabled"):
                message += "\nThreshold: " + str(cfg["threshold"]) + " joins in " + str(cfg["window_seconds"]) + "s"
            message += "\nConfigure: /raid setup"
        elif action == "tickets":
            cfg = self.store.option(guild.id, "tickets_settings", {}) or {}
            records = self.store.option(guild.id, "tickets_records", {}) or {}
            message = (
                "Native Apex Tickets: " + ("enabled" if cfg.get("enabled") else "disabled")
                + "\nOpen tickets: "
                + str(sum(record.get("status") == "open" for record in records.values()))
                + "\nConfigure: /tickets setup • /tickets panel"
            )
        elif action == "backups":
            message = (
                "Recovery module: " + (
                    "loaded" if self.bot.get_cog("Backups") else "unavailable"
                )
                + "\nUse /backups status to review verified archive coverage."
                + "\nUse /recovery_status to review channel recovery."
            )
        else:
            raise ValueError("Unknown Apex quick action.")
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=NO_MENTIONS,
        )
