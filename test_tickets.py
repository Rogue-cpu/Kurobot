"""Native Apex support ticket tests: authorisation, privacy, and persistence."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from storage import Store
from tickets import Tickets, TicketPanelView, TicketCloseView, ticket_name


class Category:
    def __init__(self, guild, category_id=3):
        self.guild = guild
        self.id = category_id


class TicketsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db.sqlite3"))
        self.member = Mock()
        self.member.id = 42
        self.member.name = "Example.User"
        self.member.roles = []
        self.role = Mock()
        self.role.id = 11
        self.role.guild = NS(id=1)
        self.role.is_default.return_value = False
        self.log_channel = Mock()
        self.log_channel.id = 12
        self.log_channel.send = AsyncMock()
        self.log_channel.permissions_for.return_value = NS(
            view_channel=True, send_messages=True, embed_links=True,
        )
        self.channel = Mock()
        self.channel.id = 300
        self.channel.mention = "<#300>"
        self.channel.send = AsyncMock()
        self.channel.set_permissions = AsyncMock()
        self.guild = Mock()
        self.guild.id = 1
        self.guild.owner_id = 5
        self.guild.me = Mock()
        self.guild.me.guild_permissions.manage_channels = True
        self.guild.default_role = Mock()
        self.category = Category(self.guild)
        def channel_for_id(cid):
            return {
                3: self.category, 12: self.log_channel, 300: self.channel,
            }.get(cid)
        self.guild.get_channel.side_effect = channel_for_id
        self.guild.get_role.return_value = self.role
        self.guild.get_member.return_value = self.member
        self.guild.create_text_channel = AsyncMock(return_value=self.channel)
        self.bot = Mock()
        self.bot.user.id = 99
        self.bot.add_view = Mock()
        self.cog = Tickets(self.bot, self.store)
        self.store.set_option(1, "tickets_settings", {
            "enabled":True,"category_id":3,"staff_role_id":11,"log_channel_id":12,
        })

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def interaction(self, user=None, channel_id=300):
        return NS(
            guild=self.guild, guild_id=1, channel_id=channel_id,
            channel=self.channel, user=user or self.member,
            response=NS(defer=AsyncMock(), send_message=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )

    def owner_interaction(self):
        i = self.interaction(user=NS(id=5))
        return i

    def test_ticket_channel_name_is_sanitized(self):
        self.assertEqual(ticket_name(NS(name="CONNOR.Test!")), "ticket-connor-test")

    async def test_persistent_views_have_stable_custom_ids(self):
        panel = TicketPanelView()
        close = TicketCloseView()
        self.assertIsNone(panel.timeout)
        self.assertIsNone(close.timeout)
        self.assertEqual(panel.children[0].custom_id, "kuro:tickets:open:v1")
        self.assertEqual(close.children[0].custom_id, "kuro:tickets:close:v1")
        await self.cog.cog_load()
        self.assertEqual(self.bot.add_view.call_count, 2)

    async def test_owner_setup_saves_guild_scoped_settings(self):
        interaction = self.owner_interaction()
        self.category.guild = self.guild
        self.role.guild = self.guild
        self.guild.id = 1
        with patch("tickets.discord.CategoryChannel", Category):
            await self.cog.setup.callback(
                self.cog, interaction, self.category, self.role, self.log_channel,
            )
        self.assertTrue(self.cog.settings(1)["enabled"])
        self.assertEqual(self.cog.settings(1)["staff_role_id"], 11)
        self.assertFalse(self.cog.settings(2).get("enabled"))

    async def test_non_owner_cannot_setup(self):
        with self.assertRaises(ValueError):
            await self.cog.setup.callback(
                self.cog, self.interaction(), self.category, self.role, self.log_channel,
            )

    async def test_open_creates_private_overwrites_and_persists(self):
        i = self.interaction()
        with patch("tickets.discord.CategoryChannel", Category):
            await self.cog.open_ticket(i)
        self.guild.create_text_channel.assert_awaited_once()
        kwargs = self.guild.create_text_channel.await_args.kwargs
        overwrite = kwargs["overwrites"]
        self.assertFalse(overwrite[self.guild.default_role].view_channel)
        self.assertTrue(overwrite[self.member].view_channel)
        self.assertTrue(overwrite[self.role].view_channel)
        self.assertEqual(self.cog.records(1)["42"]["channel_id"], 300)
        self.assertEqual(self.cog.records(1)["42"]["status"], "open")
        self.channel.send.assert_awaited_once()
        i.followup.send.assert_awaited_once()

    async def test_duplicate_open_does_not_create_second_channel(self):
        with patch("tickets.discord.CategoryChannel", Category):
            await self.cog.open_ticket(self.interaction())
            second=self.interaction()
            await self.cog.open_ticket(second)
        self.guild.create_text_channel.assert_awaited_once()
        self.assertIn("already", second.followup.send.await_args.args[0])

    async def test_disabled_ticket_creation_does_not_create_channel(self):
        self.store.set_option(1,"tickets_settings",{"enabled":False})
        await self.cog.open_ticket(self.interaction())
        self.guild.create_text_channel.assert_not_awaited()

    async def test_ticket_does_not_delete_any_channel(self):
        self.guild.get_channel = Mock(return_value=self.category)
        self.guild.delete_channel = AsyncMock()
        with patch("tickets.discord.CategoryChannel", Category):
            await self.cog.open_ticket(self.interaction())
        self.guild.delete_channel.assert_not_awaited()

    async def test_closing_ticket_locks_requester_and_preserves_history(self):
        self.store.set_option(1,"tickets_records",{
            "42":{"channel_id":300,"user_id":42,"status":"open","opened_at":1},
        })
        i=self.interaction()
        await self.cog.close_ticket(i)
        self.channel.set_permissions.assert_awaited_once()
        kwargs=self.channel.set_permissions.await_args.kwargs
        self.assertFalse(kwargs["send_messages"])
        self.assertTrue(kwargs["read_message_history"])
        self.assertEqual(self.cog.records(1)["42"]["status"],"closed")
        self.log_channel.send.assert_awaited_once()
        self.channel.delete.assert_not_awaited()

    async def test_stranger_cannot_close_another_users_ticket(self):
        self.store.set_option(1,"tickets_records",{
            "42":{"channel_id":300,"user_id":42,"status":"open","opened_at":1},
        })
        outsider=Mock()
        outsider.id=33
        outsider.roles=[]
        i=self.interaction(user=outsider)
        await self.cog.close_ticket(i)
        self.channel.set_permissions.assert_not_awaited()
        self.assertEqual(self.cog.records(1)["42"]["status"],"open")

    async def test_staff_role_can_close_ticket(self):
        self.store.set_option(1,"tickets_records",{
            "42":{"channel_id":300,"user_id":42,"status":"open","opened_at":1},
        })
        staff=Mock()
        staff.id=44
        staff.roles=[self.role]
        await self.cog.close_ticket(self.interaction(user=staff))
        self.assertEqual(self.cog.records(1)["42"]["status"],"closed")

    async def test_records_survive_database_restart(self):
        self.store.set_option(1,"tickets_records",{
            "42":{"channel_id":300,"user_id":42,"status":"open"},
        })
        self.store.db.close()
        self.store=Store(str(Path(self.tmp.name) / "db.sqlite3"))
        self.cog.store=self.store
        self.assertEqual(self.cog.records(1)["42"]["channel_id"],300)

    async def test_owner_can_disable_without_deleting_existing_tickets(self):
        self.store.set_option(1,"tickets_records",{
            "42":{"channel_id":300,"user_id":42,"status":"open"},
        })
        await self.cog.disable.callback(self.cog,self.owner_interaction())
        self.assertFalse(self.cog.settings(1)["enabled"])
        self.assertEqual(self.cog.records(1)["42"]["status"],"open")
        self.channel.delete.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
