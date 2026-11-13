"""Offline checks for the Apex menu and opt-in visual migration atlas."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from apex_hub import (
    ApexHub, ApexHubView, BOT_TARGETS, SECTIONS, STATUS_TEXT,
    build_embed, find_target, lookup_member_avatar, all_target_names,
)


class ApexHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = NS(id=123, owner_id=5, members=[])
        self.interaction = NS(
            guild=self.guild, guild_id=123, user=NS(id=5),
            response=NS(send_message=AsyncMock(), edit_message=AsyncMock()),
        )

    def test_all_32_screenshot_bots_are_mapped_once(self):
        items = all_target_names()
        self.assertEqual(len(items), 32)
        self.assertEqual(len(set(items)), 32)
        for expected in (
            ".fmbot", "Appy", "Arcane", "BibleBot", "Confessions",
            "Conquest", "Daedalus", "DISBOARD", "Discodus",
            "DiscordServers", "Dyno", "EazyAutodelete",
            "GiveawayBot", "Jockie Music", "kesarAI", "Kuro’s Pet",
            "Make it a Quote", "ModMail", "Security", "Server Tag Manager",
            "Ticket Tool", "Tupperbox", "Vetra", "YAGPDB.xyz",
            "Discohook Utils", "Emoji Stealer", "Honeypot",
            "InviteLogger", "SnipeBot", "TTS Bot", "Xenon", "YouTube",
        ):
            self.assertIn(expected, items)

    def test_sections_and_options_fit_discord_component_limits(self):
        self.assertLessEqual(len(SECTIONS), 25)
        self.assertLessEqual(len(SECTIONS), 25)
        for key, entries in BOT_TARGETS.items():
            self.assertIn(key, SECTIONS)
            self.assertLessEqual(len(entries), 25)
            for name, alias, state, note in entries:
                self.assertIn(state, STATUS_TEXT)
                self.assertLessEqual(len(name), 100)
                self.assertTrue(note)

    def test_overview_and_category_embeds_stay_under_discord_limits(self):
        for section in SECTIONS:
            embed = build_embed(section, self.guild)
            self.assertLessEqual(len(embed), 6000)
            self.assertTrue(embed.title)
            self.assertLessEqual(len(embed.fields), 25)

    def test_target_details_are_grounded_and_clearly_labeled(self):
        for section, entries in BOT_TARGETS.items():
            for item in entries:
                embed = build_embed(section, self.guild, item)
                self.assertIn(item[0], embed.title)
                self.assertIn(STATUS_TEXT[item[2]], [x.value for x in embed.fields])
                self.assertLessEqual(len(embed), 6000)

    def test_avatar_is_from_installed_bot_only(self):
        self.guild.members = [
            NS(bot=False, display_name="Dyno", name="Dyno",
               global_name="Dyno", display_avatar=NS(url="https://example.com/human.png")),
            NS(bot=True, display_name="Dyno", name="Dyno", global_name="Dyno",
               display_avatar=NS(url="https://example.com/dyno.png")),
        ]
        self.assertEqual(
            lookup_member_avatar(self.guild, "Dyno"),
            "https://example.com/dyno.png",
        )
        self.assertIsNone(lookup_member_avatar(self.guild, "NotInstalled"))
        self.assertEqual(
            build_embed("moderation", self.guild, find_target("Dyno", "moderation")).thumbnail.url,
            "https://example.com/dyno.png",
        )

    def test_view_rebuild_does_not_duplicate_controls(self):
        view = ApexHubView(5, 123)
        self.assertEqual(len(view.children), 3)
        view.section = "security"
        view.rebuild()
        self.assertEqual(len(view.children), 4)
        view.target = "Security"
        view.rebuild()
        self.assertEqual(len(view.children), 4)
        view.section = "atlas"
        view.rebuild()
        self.assertEqual(len(view.children), 3)

    async def test_view_rejects_other_users_and_guilds(self):
        view = ApexHubView(5, 123)
        self.assertTrue(await view.interaction_check(self.interaction))
        wrong = NS(
            guild=self.guild, guild_id=123, user=NS(id=6),
            response=NS(send_message=AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(wrong))
        wrong.response.send_message.assert_awaited_once()
        crossguild = NS(
            guild=NS(owner_id=5), guild_id=999,
            user=NS(id=5), response=NS(send_message=AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(crossguild))

    async def test_hub_requires_server_owner(self):
        cog = ApexHub(NS())
        self.interaction.user.id = 8
        with self.assertRaises(ValueError):
            await cog.hub.callback(cog, self.interaction)
        self.interaction.response.send_message.assert_not_awaited()

    async def test_owner_hub_opens_ephemeral_menu(self):
        cog = ApexHub(NS())
        await cog.hub.callback(cog, self.interaction)
        call = self.interaction.response.send_message.await_args
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertIsInstance(call.kwargs["view"], ApexHubView)
        self.assertIn("Apex", call.kwargs["embed"].title)


if __name__ == "__main__":
    unittest.main()
