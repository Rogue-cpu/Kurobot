import asyncio
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

from features import Features, VerifyModal, check_ban, name_matches, parse_deathnote
from storage import Store


def member(user_id, name="person", display="Person", role=1, can_ban=True):
    return SimpleNamespace(id=user_id, name=name, display_name=display, top_role=role,
                           guild_permissions=SimpleNamespace(ban_members=can_ban))


class ParsingTests(unittest.TestCase):
    def test_deathnote_input_keeps_reason_separators(self):
        self.assertEqual(parse_deathnote("Kuro | 2h | reason | extra"), ("Kuro", 7200, "reason | extra"))
        for text in ("Kuro", "Kuro | 0s | reason", "| 1h | reason", "Kuro | 1h |"):
            with self.assertRaises(ValueError):
                parse_deathnote(text)

    def test_duplicate_names_require_selection_and_ids_are_exact(self):
        members = [member(10, "one", "Kuro"), member(20, "two", "Kuro")]
        self.assertEqual(len(name_matches(members, "kuro")), 2)
        self.assertEqual(name_matches(members, "<@20>"), [members[1]])
        self.assertEqual(name_matches(members, "<@!10>"), [members[0]])
        self.assertEqual(name_matches(members, "999"), [])
        self.assertTrue(name_matches(members, "kuroo"))

    def test_permissions_and_role_order_are_enforced(self):
        guild = SimpleNamespace(owner_id=1, me=member(2, role=10))
        actor = member(3, role=5)
        check_ban(guild, actor, member(4, role=1), 2)
        for target in (member(1), member(2), actor, member(4, role=5)):
            with self.assertRaises(ValueError):
                check_ban(guild, actor, target, 2)
        with self.assertRaises(ValueError):
            check_ban(guild, member(3, can_ban=False), member(4), 2)


class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "kuro.db"))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_networks_require_valid_single_use_invites(self):
        network = self.store.create_network(1, "Friends")
        code = self.store.invite("network", network)
        with self.assertRaises(ValueError):
            self.store.consume_invite("chat", code)
        self.assertEqual(self.store.join_network(2, code), network)
        with self.assertRaises(ValueError):
            self.store.join_network(3, code)
        self.assertIsNone(self.store.network(3))
        code = self.store.invite("network", network)
        self.store.db.execute("UPDATE invitations SET expires=0")
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.join_network(3, code)

    def test_global_bans_are_isolated_and_network_can_dissolve(self):
        network = self.store.create_network(1, "Friends")
        self.store.join_network(2, self.store.invite("network", network))
        self.store.create_network(3, "Other")
        with self.store.db:
            self.store.db.execute("INSERT INTO global_bans VALUES (?,?,?,?)", (network, 88, "reason", 9))
        self.assertIsNotNone(self.store.global_ban(2, 88))
        self.assertIsNone(self.store.global_ban(3, 88))
        self.store.set_whitelist(2, 88, True)
        self.assertTrue(self.store.whitelisted(2, 88))
        self.assertFalse(self.store.whitelisted(1, 88))
        self.store.leave_network(1)
        self.assertIsNone(self.store.network(2))
        self.assertIsNone(self.store.global_ban(2, 88))

    def test_recovery_remaps_dependents_even_after_gateway_create(self):
        self.store.snapshot(1, 10, {"type": 4})
        self.store.snapshot(1, 20, {"type": 4})
        self.store.snapshot(1, 30, {"category": 10})
        self.store.remap_channel(1, 10, 20)
        self.assertEqual(dict(self.store.snapshots(1))[30]["category"], 20)
        self.store.set_option(1, "verification", {"channel": 30, "role": 9})
        self.store.set_option(1, "deathnote_channel", 30)
        self.store.db.execute("INSERT INTO chat_channels VALUES (30,1,'room')")
        self.store.db.commit()
        self.store.archive(1, 30, 1, 1, {"id": 1, "content": "hello"})
        self.store.remap_channel(1, 30, 40)
        self.assertEqual(self.store.option(1, "verification")["channel"], 40)
        self.assertEqual(self.store.option(1, "deathnote_channel"), 40)
        self.assertEqual(self.store.messages(1, 40)[0]["content"], "hello")
        self.assertEqual(self.store.db.execute("SELECT channel FROM chat_channels").fetchone()[0], 40)

    def test_archive_is_bounded_edits_replace_and_guilds_are_isolated(self):
        for number in range(1002):
            self.store.archive(1, 2, number, number, {"id": number})
        self.assertEqual(len(self.store.messages(1, 2)), 1000)
        self.assertEqual(self.store.messages(1, 2)[0]["id"], 2)
        self.store.archive(1, 2, 1001, 1001, {"id": 1001, "content": "edited"})
        self.assertEqual(self.store.messages(1, 2)[-1]["content"], "edited")
        self.assertEqual(self.store.messages(9, 2), [])


class AsyncFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "db.sqlite3"))
        self.bot = SimpleNamespace(data_dir=Path(self.temp.name), user=member(99), intents=SimpleNamespace(message_content=True))
        self.features = Features(self.bot, self.store, lambda role, guild: True)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_shared_ban_whitelist_and_hierarchy_block_api_calls(self):
        guild = SimpleNamespace(id=1, owner_id=2, me=member(99, role=5), ban=AsyncMock(), fetch_member=AsyncMock(return_value=member(3, role=6)))
        row = {"network": "friends", "member": 3, "actor": 4, "reason": "reason"}
        self.store.set_whitelist(1, 3, True)
        self.assertEqual(await self.features.enforce_ban(guild, row), "whitelisted")
        guild.ban.assert_not_called()
        self.store.set_whitelist(1, 3, False)
        self.assertEqual(await self.features.enforce_ban(guild, row), "role hierarchy blocked")
        guild.ban.assert_not_called()
        guild.fetch_member.return_value = member(3, role=1)
        self.assertEqual(await self.features.enforce_ban(guild, row), "banned")
        guild.ban.assert_awaited_once()

    async def test_recovery_restores_parent_first_and_does_not_clone_existing(self):
        self.store.set_option(1, "recovery", {"enabled": True})
        self.store.snapshot(1, 10, {"type": 4, "position": 0})
        self.store.snapshot(1, 20, {"type": 0, "position": 1})
        self.store.snapshot(1, 30, {"type": 0, "position": 2})
        guild = SimpleNamespace(id=1, fetch_channels=AsyncMock(return_value=[SimpleNamespace(id=30)]))
        self.features.restore_channel = AsyncMock(return_value=SimpleNamespace(id=100))
        await self.features.reconcile(guild)
        self.assertEqual([call.args[1] for call in self.features.restore_channel.await_args_list], [10])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM recovery_jobs WHERE status='pending'").fetchone()[0],1)

    async def test_recovery_disabled_does_not_call_discord(self):
        guild = SimpleNamespace(id=1, fetch_channels=AsyncMock())
        await self.features.reconcile(guild)
        guild.fetch_channels.assert_not_called()

    async def test_private_source_channel_is_never_relayed(self):
        self.store.db.execute("INSERT INTO chat_channels VALUES (1,2,'room')")
        self.store.db.commit()
        channel = Mock(spec=discord.TextChannel)
        channel.id = 1
        channel.permissions_for.return_value.view_channel = False
        message = SimpleNamespace(channel=channel, guild=SimpleNamespace(default_role=object()))
        await self.features.relay_message(message)

    async def test_expired_or_wrong_user_verification_cannot_assign_role(self):
        modal = VerifyModal(self.features, 3, 1)
        modal.deadline = time.monotonic() - 1
        interaction = SimpleNamespace(user=member(3), guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        self.assertIn("expired", interaction.response.send_message.await_args.args[0])

    async def test_verification_success_adds_only_configured_role(self):
        self.store.set_option(1, "verification", {"role": 8, "min_days": 7})
        modal = VerifyModal(self.features, 3, 1)
        modal.answer._value = str(modal.a + modal.b)
        role = object()
        target = SimpleNamespace(id=3, created_at=discord.utils.utcnow()-timedelta(days=30), add_roles=AsyncMock())
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=target), get_role=Mock(return_value=role), me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)))
        interaction = SimpleNamespace(user=member(3), guild_id=1, guild=guild, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await modal.on_submit(interaction)
        target.add_roles.assert_awaited_once_with(role, reason="Kuro verification completed")
        self.assertTrue(modal.used)
        self.assertIn("Verified", interaction.followup.send.await_args.args[0])

    async def test_verification_age_limit_blocks_role(self):
        self.store.set_option(1, "verification", {"role": 8, "min_days": 7})
        modal = VerifyModal(self.features, 3, 1)
        modal.answer._value = str(modal.a + modal.b)
        target = SimpleNamespace(id=3, created_at=discord.utils.utcnow(), add_roles=AsyncMock())
        interaction = SimpleNamespace(user=member(3), guild_id=1, guild=SimpleNamespace(fetch_member=AsyncMock(return_value=target)), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await modal.on_submit(interaction)
        target.add_roles.assert_not_called()
        self.assertIn("7 days", interaction.followup.send.await_args.args[0])

    async def test_chat_delivery_disables_mentions_and_skips_private_destinations(self):
        self.store.db.executemany("INSERT INTO chat_channels VALUES (?,?,?)", [(1,2,'room'), (3,4,'room'), (5,6,'room')])
        self.store.db.commit()
        source = Mock(spec=discord.TextChannel)
        source.id, source.name = 1, "shared"
        source.permissions_for.return_value.view_channel = True
        target = Mock(spec=discord.TextChannel)
        target.permissions_for.return_value.view_channel = True
        target.send = AsyncMock()
        private = Mock(spec=discord.TextChannel)
        private.permissions_for.return_value.view_channel = False
        private.send = AsyncMock()
        self.bot.get_channel = lambda channel_id: {3: target, 5: private}[channel_id]
        message = SimpleNamespace(channel=source, guild=SimpleNamespace(id=2, name="Source", default_role=object()), author=member(7), content="@everyone hello", attachments=[])
        await self.features.relay_message(message)
        target.send.assert_awaited_once()
        self.assertFalse(target.send.await_args.kwargs["allowed_mentions"].everyone)
        self.assertFalse(target.send.await_args.kwargs["allowed_mentions"].users)
        private.send.assert_not_called()

    async def test_wrong_answer_cannot_verify(self):
        modal = VerifyModal(self.features, 3, 1)
        modal.answer._value = "999"
        interaction = SimpleNamespace(user=member(3), guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        self.assertTrue(modal.used)
        self.assertIn("Incorrect", interaction.response.send_message.await_args.args[0])

    async def test_cog_commands_register_offline(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.data_dir = Path(self.temp.name)
        async with bot:
            await bot.add_cog(Features(bot, self.store, lambda role, guild: True))
            names = {command.name for command in bot.tree.get_commands()}
            self.assertTrue({"verification_setup", "globalban_add", "chat_join", "recovery_setup", "deathnote_channel"} <= names)


if __name__ == "__main__":
    unittest.main()
