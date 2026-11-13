"""Apex bot inventory and readiness: no other bots are changed by this module."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from apex import Apex, bot_inventory_line, privilege_labels
from storage import Store


def permissions(**changes):
    defaults = dict(
        administrator=False, manage_guild=False, manage_roles=False,
        manage_channels=False, manage_webhooks=False, ban_members=False,
    )
    defaults.update(changes)
    return NS(**defaults)


class ApexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db.sqlite3"))
        self.bot = NS(
            user=NS(id=99),
            intents=NS(members=True),
            get_cog=Mock(return_value=NS()),
        )
        self.cog = Apex(self.bot, self.store)
        self.guild = NS(id=1, owner_id=5, kick=AsyncMock(), ban=AsyncMock())
        self.users = [
            NS(id=99, bot=True, display_name="Kurobot", guild_permissions=permissions(administrator=True)),
            NS(id=13, bot=True, display_name="UtilityBot", guild_permissions=permissions(manage_guild=True)),
            NS(id=7, bot=False, display_name="A human", guild_permissions=permissions(administrator=True)),
        ]

        async def members(limit):
            for member in self.users:
                yield member

        self.guild.fetch_members = members
        self.response = NS(defer=AsyncMock(), send_message=AsyncMock())
        self.followup = NS(send=AsyncMock())

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def interaction(self, owner_id=5):
        return NS(
            guild=self.guild,
            guild_id=1,
            user=NS(id=owner_id),
            response=self.response,
            followup=self.followup,
        )

    async def test_owner_inventory_lists_bot_only(self):
        await self.cog.inventory.callback(self.cog, self.interaction())
        data = self.followup.send.await_args.args[0]
        self.assertIn("Kurobot", data)
        self.assertIn("UtilityBot", data)
        self.assertNotIn("A human", data)
        self.assertIn("read-only".upper(), data)
        self.assertEqual(len(self.users), 3)
        self.guild.kick.assert_not_awaited()
        self.guild.ban.assert_not_awaited()

    async def test_non_owner_cannot_read_inventory(self):
        with self.assertRaises(ValueError):
            await self.cog.inventory.callback(self.cog, self.interaction(owner_id=8))
        self.response.defer.assert_not_awaited()

    async def test_member_intent_required(self):
        self.bot.intents.members = False
        with self.assertRaises(ValueError):
            await self.cog.inventory.callback(self.cog, self.interaction())

    async def test_dangerous_permissions_are_reported(self):
        names = privilege_labels(self.users[1])
        self.assertIn("Manage Server", names)
        self.assertNotIn("Administrator", names)

    async def test_admin_permission_does_not_repeat_every_implied_power(self):
        self.assertEqual(privilege_labels(self.users[0]), ["Administrator"])

    async def test_untrusted_display_names_are_escaped(self):
        member = NS(
            id=77, display_name="**dangerous** name", bot=True,
            guild_permissions=permissions(),
        )
        self.assertIn(r"\*\*dangerous\*\*", bot_inventory_line(member))

    async def test_readiness_never_changes_guild_or_settings(self):
        self.store.set_option(1, "vanity_guard", {"enabled":True, "response":"alert"})
        await self.cog.readiness.callback(self.cog, self.interaction())
        text = self.response.send_message.await_args.args[0]
        self.assertIn("Vanity Guard: on (alert)", text)
        self.assertIn("Anti-nuke alerts: off", text)
        self.assertIn("Raid alerts: off", text)
        self.guild.kick.assert_not_awaited()
        self.guild.ban.assert_not_awaited()
        self.assertEqual(self.store.option(1, "vanity_guard")["response"], "alert")

    async def test_permissions_are_scoped_per_guild(self):
        self.store.set_option(2, "vanity_guard", {"enabled":True, "response":"quarantine"})
        await self.cog.readiness.callback(self.cog, self.interaction())
        body=self.response.send_message.await_args.args[0]
        self.assertIn("Vanity Guard: off", body)
        self.assertNotIn("quarantine", body)

    async def test_missing_module_shows_not_installed(self):
        self.bot.get_cog.return_value = None
        await self.cog.readiness.callback(self.cog, self.interaction())
        text = self.response.send_message.await_args.args[0]
        self.assertIn("✗ Anti-nuke alerts", text)


if __name__ == "__main__":
    unittest.main()
