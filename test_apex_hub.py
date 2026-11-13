"""Tests for the Apex functional system UI, not competitor-bot directory."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from apex_hub import (
    ApexHub, ApexHubView, FEATURES, SECTIONS, STATUS,
    feature_embed,
)


class ApexHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = NS(id=123, owner_id=5, members=[])
        self.bot = NS(
            user=None,
            get_cog=Mock(return_value=NS()),
        )
        self.store = NS(option=Mock(return_value={}))
        self.interaction = NS(
            guild=self.guild, guild_id=123, user=NS(id=5),
            response=NS(send_message=AsyncMock(), edit_message=AsyncMock()),
        )

    def test_functional_sections_fit_discord_component_limits(self):
        self.assertLessEqual(len(SECTIONS), 25)
        self.assertEqual(set(FEATURES), set(SECTIONS)-{"overview"})
        for section, items in FEATURES.items():
            self.assertLessEqual(len(items), 25)
            for key, item in items.items():
                self.assertLessEqual(len(item[0]), 100)
                self.assertIn(item[1], STATUS)
                self.assertTrue(item[2])
                self.assertIsInstance(item[3], str)

    def test_all_module_embeds_within_discord_limits(self):
        for section in SECTIONS:
            embed = feature_embed(section)
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.fields), 25)
            for key in FEATURES.get(section, {}):
                detail = feature_embed(section, key)
                self.assertLessEqual(len(detail), 6000)

    def test_no_competitor_bot_directory_in_hub(self):
        for section, items in FEATURES.items():
            self.assertNotIn("atlas", section)
            self.assertNotIn("bot directory", feature_embed(section).description.lower())
        self.assertIn("One bot", feature_embed("overview").description)

    def test_current_and_planned_actions_are_separate(self):
        self.assertIsNotNone(FEATURES["tickets"]["open"][4])
        self.assertIsNotNone(FEATURES["security"]["vanity"][4])
        self.assertIsNone(FEATURES["tickets"]["modmail"][4])
        self.assertIsNone(FEATURES["media"]["audio"][4])
        self.assertIn("Not yet implemented", STATUS["planned"])

    def test_panel_rebuilds_without_stacking_menu_controls(self):
        view = ApexHubView(5, 123)
        self.assertEqual(len(view.children), 3)
        view.section = "security"
        view.rebuild()
        self.assertEqual(len(view.children), 4)
        view.feature = "vanity"
        view.rebuild()
        self.assertEqual(len(view.children), 5)
        view.feature = "advanced"
        view.rebuild()
        self.assertEqual(len(view.children), 4)

    async def test_owner_access_only(self):
        view = ApexHubView(5, 123)
        self.assertTrue(await view.interaction_check(self.interaction))
        attacker = NS(
            user=NS(id=6), guild_id=123, guild=self.guild,
            response=NS(send_message=AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(attacker))
        attacker.response.send_message.assert_awaited_once()

    async def test_hub_cannot_be_opened_by_other_member(self):
        cog = ApexHub(self.bot, self.store)
        self.interaction.user.id = 8
        with self.assertRaises(ValueError):
            await cog.hub.callback(cog, self.interaction)
        self.interaction.response.send_message.assert_not_awaited()

    async def test_owner_opens_ephemeral_hub(self):
        cog = ApexHub(self.bot, self.store)
        await cog.hub.callback(cog, self.interaction)
        kw=self.interaction.response.send_message.await_args.kwargs
        self.assertTrue(kw["ephemeral"])
        self.assertIsInstance(kw["view"], ApexHubView)
        self.assertIn("Apex", kw["embed"].title)

    async def test_vanity_action_checks_real_stored_baseline_and_live_code(self):
        guard=NS(config=Mock(return_value={"enabled": True,"code":"tpusa","response":"alert"}),
                 current_code=AsyncMock(return_value="tpusa"))
        self.bot.get_cog.return_value=guard
        cog = ApexHub(self.bot,self.store)
        await cog.run_quick_action(self.interaction,"vanity")
        msg=self.interaction.response.send_message.await_args.args[0]
        self.assertIn("MATCH", msg)
        self.assertIn("tpusa", msg)

    async def test_raid_status_does_not_modify_settings(self):
        self.store.option.return_value={"enabled":True,"threshold":8,"window_seconds":60}
        cog=ApexHub(self.bot,self.store)
        await cog.run_quick_action(self.interaction,"raid")
        msg=self.interaction.response.send_message.await_args.args[0]
        self.assertIn("8 joins",msg)
        self.store.option.assert_called_with(123,"raid_alert_config",{})

    async def test_ticket_open_action_invokes_native_cog(self):
        tickets=NS(open_ticket=AsyncMock())
        self.bot.get_cog.return_value=tickets
        cog=ApexHub(self.bot,self.store)
        await cog.run_quick_action(self.interaction,"open_ticket")
        tickets.open_ticket.assert_awaited_once_with(self.interaction)

    async def test_unknown_action_cannot_trigger_operations(self):
        cog=ApexHub(self.bot,self.store)
        with self.assertRaises(ValueError):
            await cog.run_quick_action(self.interaction,"delete_everything")
        self.interaction.response.send_message.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
