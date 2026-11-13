"""Apex command hub and visual bot atlas for consensual server migrations.

This is a navigation interface, not a replacement for existing Discord commands.
Images are live guild bot avatars for identification, never copied branding assets.
"""
import discord
from discord import app_commands
from discord.ext import commands

from features import NO_MENTIONS, manager, require_owner

# Names originate from the server-member screenshots supplied by the owner.
# Labels describe migration work, not a claim of feature parity.
BOT_TARGETS = {
    "security": (
        ("Security", "Security", "partial", "Anti-nuke and role security need live tests"),
        ("Honeypot", "Honeypot", "missing", "Honeypot and suspect-join workflows need research"),
        ("InviteLogger", "InviteLogger", "missing", "Invite attribution and re-join alerts need implementation"),
        ("SnipeBot", "SnipeBot", "missing", "Message deletion review and retention policies need design"),
    ),
    "moderation": (
        ("Dyno", "Dyno", "partial", "Basic moderation exists; automod and custom actions remain"),
        ("YAGPDB.xyz", "YAGPDB.xyz", "partial", "Some roles and moderation exist; complex automations remain"),
        ("EazyAutodelete", "EazyAutodelete", "missing", "Scheduled channel cleanup is not yet built"),
        ("Vetra", "Vetra", "research", "Exact enabled functions must be audited first"),
    ),
    "support": (
        ("Appy", "Appy", "missing", "Applications and review workflows need implementation"),
        ("ModMail", "ModMail", "missing", "Private staff-mail routing needs implementation"),
        ("Ticket Tool", "Ticket Tool", "missing", "Tickets, permissions and transcript workflows need implementation"),
        ("Confessions", "Confessions", "research", "Verify the actual anonymous posting functions"),
    ),
    "community": (
        ("Arcane", "Arcane", "partial", "Levels and rewards exist, not all Arcane features"),
        ("GiveawayBot", "GiveawayBot", "missing", "Giveaway management needs implementation"),
        ("Kuro’s Pet", "Kuro’s Pet", "research", "Inspect the particular pet bot's gameplay and commands"),
        ("Conquest", "Conquest", "research", "Inspect the particular bot's purpose and commands"),
        ("Server Tag Manager", "Server Tag Manager", "research", "Check supported Discord server-tag API features"),
    ),
    "media": (
        (".fmbot", ".fmbot", "missing", "Music statistics may require an authorized external API"),
        ("Jockie Music", "Jockie Music", "missing", "Playback depends on supported and licensed music sources"),
        ("YouTube", "YouTube", "missing", "YouTube alerts/integrations require an authorized API"),
        ("TTS Bot", "TTS Bot", "missing", "Text-to-speech voice features are not yet built"),
        ("Make it a Quote", "Make it a Quote", "missing", "Quote-card generation needs implementation"),
    ),
    "creative": (
        ("Discohook Utils", "Discohook Utils", "missing", "Embed composing and utility integrations remain"),
        ("Emoji Stealer", "Emoji Stealer", "missing", "Authorized emoji import and permission checks remain"),
        ("Tupperbox", "Tupperbox", "missing", "Roleplay proxy messaging needs consent and impersonation safeguards"),
        ("kesarAI", "kesarAI", "research", "Review specific AI commands and their data handling"),
        ("BibleBot", "BibleBot", "missing", "Bible references require suitable translation permissions"),
    ),
    "discovery": (
        ("DISBOARD", "DISBOARD", "external", "Its public directory audience cannot be reproduced internally"),
        ("Discodus", "Discodus", "external", "Third-party public listings are separate services"),
        ("DiscordServers", "DiscordServers", "external", "Third-party public listings are separate services"),
    ),
    "backups": (
        ("Xenon", "Xenon", "partial", "Backups and blueprints exist, with known recovery limits"),
        ("Daedalus", "Daedalus", "research", "Determine the bot's actual duties before migration"),
    ),
}

SECTIONS = {
    "overview": ("Apex Command Center", "🏛️", "One place to find Apex tools."),
    "security": ("Security & Anti-Nuke", "🛡️", "Vanity guard, audit monitoring, raids, owner response."),
    "moderation": ("Moderation & Automation", "⚖️", "Warnings, bans, timed jail, cleanup and custom automod."),
    "support": ("Tickets & Applications", "🎫", "Staff contact, applications and case workflows."),
    "community": ("Community & Levels", "✨", "Onboarding, XP, roles, giveaways and games."),
    "media": ("Music & Media", "🎵", "Audio, TTS, music statistics and publishing."),
    "creative": ("Creative & Utility", "🧩", "Embeds, emoji, proxies, AI and reference tools."),
    "discovery": ("Discovery & Promotion", "📡", "External listing and promotional integrations."),
    "backups": ("Backups & Blueprints", "🗄️", "Snapshots, recovery, import and KuroGene."),
    "atlas": ("Bot Atlas", "🧭", "Browse bot identities and migration status."),
}

# The command strings below are help text, never executed by button callbacks.
COMMANDS = {
    "security": (
        "/vanity status • /vanity risk • /vanity response",
        "/antinuke setup • /antinuke status • /antinuke incidents",
        "/raid setup • /raid status • /protection lock",
    ),
    "moderation": (
        "/tools warn • /tools warnings • /tools purge",
        "/deathnote • /deathnote_cancel • /jail add",
        "/network_create • /globalban_add • /whitelist",
    ),
    "support": (
        "Dedicated application, modmail and ticket commands: planned",
    ),
    "community": (
        "/verification_setup • /roles_setup • /selfrole pick",
        "/levels setup • /levels leaderboard • /levels reward",
    ),
    "media": ("Music, speech, video and quote tools: planned",),
    "creative": ("Embed composition, roleplay proxies and AI utilities: planned",),
    "discovery": ("External directory and listing actions remain separate services",),
    "backups": (
        "/backups create • /backups verify • /backup_export",
        "/recovery_status • /blueprint create • /blueprint import",
        "/recovery_plus history • /recovery_retry",
    ),
}

STATUS_TEXT = {
    "partial": "🟡 Partial Apex functionality",
    "missing": "⚪ Not yet implemented in Apex",
    "research": "🔎 Requires source-bot feature audit",
    "external": "🔗 External service, not fully replaceable",
}

def all_target_names():
    return [item[0] for entries in BOT_TARGETS.values() for item in entries]


def find_target(key, section):
    for item in BOT_TARGETS.get(section, ()):
        if item[0] == key:
            return item
    return None


def lookup_member_avatar(guild, display_name):
    """Use the actual installed bot's avatar, never fabricated brand artwork."""
    expected = display_name.casefold().replace("’", "'")
    for member in getattr(guild, "members", ()):
        if not getattr(member, "bot", False):
            continue
        names = (
            str(getattr(member, "display_name", "")),
            str(getattr(member, "name", "")),
            str(getattr(member, "global_name", "")),
        )
        if any(name.casefold().replace("’", "'") == expected for name in names):
            avatar = getattr(member, "display_avatar", None)
            return str(avatar.url) if avatar is not None else None
    return None


def build_embed(section, guild, target=None):
    title, icon, description = SECTIONS[section]
    embed = discord.Embed(
        title=icon + "  " + title,
        description=description,
        color=discord.Color.from_rgb(187, 27, 50),
    )
    if section == "overview":
        embed.add_field(
            name="Choose a system",
            value="Use the menu below to open Security, Moderation, Support, "
            "Community, Media, Creative, Discovery or Backups.",
            inline=False,
        )
        embed.add_field(
            name="Bot Atlas",
            value=str(len(all_target_names())) + " bot identities from your screenshots. "
            "Open a system, then choose a bot to inspect migration coverage "
            "and its installed avatar.",
            inline=False,
        )
        embed.add_field(
            name="Controls",
            value="This first version is a navigation and information hub. "
            "It does **not** run destructive commands from a menu.",
            inline=False,
        )
    elif section == "atlas":
        embed.add_field(
            name="How to browse",
            value="Pick a module using the main menu, then select a bot in "
            "the second menu to see its coverage and live avatar.",
            inline=False,
        )
        for key, (label, emoji, _) in SECTIONS.items():
            if key not in ("overview", "atlas"):
                embed.add_field(
                    name=emoji + " " + label,
                    value=", ".join(item[0] for item in BOT_TARGETS[key]),
                    inline=False,
                )
    elif target:
        name, alias, coverage, note = target
        embed.add_field(name="Migration state", value=STATUS_TEXT[coverage], inline=False)
        embed.add_field(name="Next work", value=note, inline=False)
        embed.add_field(
            name="Apex command area",
            value="\n".join(COMMANDS[section]), inline=False,
        )
        avatar = lookup_member_avatar(guild, alias)
        if avatar:
            embed.set_thumbnail(url=avatar)
            embed.add_field(
                name="Visual reference",
                value="Avatar from an installed bot in this server, shown only "
                "to identify the migration source.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Visual reference",
                value="Avatar unavailable from the member cache. "
                "No unofficial image has been substituted.",
                inline=False,
            )
        embed.title = "🧭 " + name + " → Apex"
    else:
        embed.add_field(
            name="Available / planned commands",
            value="\n".join(COMMANDS[section]),
            inline=False,
        )
        embed.add_field(
            name="Bot identities to review",
            value="\n".join(
                item[0] + " | " + STATUS_TEXT[item[2]]
                for item in BOT_TARGETS[section]
            ),
            inline=False,
        )
    embed.set_footer(
        text="Apex development hub • Owner-only • Navigation is not feature parity"
    )
    return embed


class SectionSelect(discord.ui.Select):
    def __init__(self, current):
        options = [
            discord.SelectOption(
                label=label, value=key, emoji=emoji,
                description=description[:95],
                default=key == current,
            )
            for key, (label, emoji, description) in SECTIONS.items()
        ]
        super().__init__(
            placeholder="Open an Apex system…",
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        view = self.view
        view.section = self.values[0]
        view.target = None
        view.rebuild()
        await interaction.response.edit_message(
            embed=build_embed(view.section, interaction.guild), view=view
        )


class SourceBotSelect(discord.ui.Select):
    def __init__(self, section, current=None):
        items = BOT_TARGETS[section]
        options = [
            discord.SelectOption(
                label=item[0][:100], value=item[0], default=item[0] == current,
                description=STATUS_TEXT[item[2]][2:][:95],
            )
            for item in items
        ]
        super().__init__(
            placeholder="Inspect a bot and its avatar…",
            options=options,
            row=1,
        )

    async def callback(self, interaction):
        view = self.view
        view.target = self.values[0]
        view.rebuild()
        await interaction.response.edit_message(
            embed=build_embed(
                view.section, interaction.guild,
                find_target(view.target, view.section),
            ),
            view=view,
        )


class HomeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Home", style=discord.ButtonStyle.secondary,
            emoji="🏠", row=2,
        )

    async def callback(self, interaction):
        view = self.view
        view.section = "overview"
        view.target = None
        view.rebuild()
        await interaction.response.edit_message(
            embed=build_embed("overview", interaction.guild), view=view
        )


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close", style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction):
        self.view.stop()
        await interaction.response.edit_message(
            content="Apex command center closed.",
            embed=None, view=None,
        )


class ApexHubView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.section = "overview"
        self.target = None
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(SectionSelect(self.section))
        if self.section in BOT_TARGETS:
            self.add_item(SourceBotSelect(self.section, self.target))
        self.add_item(HomeButton())
        self.add_item(CloseButton())

    async def interaction_check(self, interaction):
        allowed = (
            interaction.user.id == self.owner_id
            and interaction.guild_id == self.guild_id
            and interaction.user.id == interaction.guild.owner_id
        )
        if not allowed:
            await interaction.response.send_message(
                "This Apex control panel belongs to the server owner who opened it.",
                ephemeral=True,
            )
        return allowed


class ApexHub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="hub",
        description="Open the Apex control center and bot migration atlas.",
    )
    @manager
    async def hub(self, interaction: discord.Interaction):
        require_owner(interaction)
        view = ApexHubView(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(
            embed=build_embed("overview", interaction.guild),
            view=view, ephemeral=True, allowed_mentions=NO_MENTIONS,
        )
