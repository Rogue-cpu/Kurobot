"""Tests for the non-punitive join-burst monitor."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord

from raid_guard import RaidGuard
from storage import Store


class RaidGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "store.sqlite3"))
        self.bot = NS(intents=NS(members=True))
        self.guard = RaidGuard(self.bot, self.store)
        self.channel = NS(
            id=10, mention="<#10>", send=AsyncMock(),
            permissions_for=Mock(return_value=NS(view_channel=True, send_messages=True)),
        )
        self.guild = NS(
            id=1, owner_id=5, me=NS(),
            get_channel=Mock(return_value=self.channel),
        )
        self.store.set_option(1, "raid_alert_config", {
            "enabled": True, "channel_id": 10,
            "threshold": 5, "window_seconds": 60,
        })

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def member(self, member_id, days_old=100, bot=False, guild=None):
        return NS(
            id=member_id, bot=bot, guild=guild or self.guild,
            created_at=discord.utils.utcnow() - timedelta(days=days_old),
        )

    def interaction(self, actor_id=5):
        return NS(
            guild_id=1, guild=self.guild, user=NS(id=actor_id),
            response=NS(send_message=AsyncMock()),
        )

    async def test_five_unique_joins_trigger_one_alert(self):
        for i in range(4):
            await self.guard.inspect_join(self.member(i + 100))
        self.channel.send.assert_not_awaited()
        await self.guard.inspect_join(self.member(104))
        self.channel.send.assert_awaited_once()
        self.assertIn("5 unique human accounts", self.channel.send.await_args.args[0])

    async def test_repeated_join_event_ignored(self):
        for i in range(10):
            await self.guard.inspect_join(self.member(100))
        self.assertEqual(len(self.guard.recent(1)), 1)
        self.channel.send.assert_not_awaited()

    async def test_bot_join_excluded(self):
        await self.guard.inspect_join(self.member(100, bot=True))
        self.assertEqual(self.guard.recent(1), [])

    async def test_new_account_count_is_descriptive_not_enforcement(self):
        for i in range(5):
            await self.guard.inspect_join(self.member(100 + i, days_old=1 if i < 3 else 120))
        msg = self.channel.send.await_args.args[0]
        self.assertIn("3 were created within", msg)
        self.assertIn("not proof of a raid", msg)

    async def test_monitor_never_bans_or_kicks(self):
        self.guild.kick = AsyncMock()
        self.guild.ban = AsyncMock()
        for i in range(5):
            await self.guard.inspect_join(self.member(100 + i))
        self.guild.kick.assert_not_awaited()
        self.guild.ban.assert_not_awaited()

    async def test_alert_cooldown(self):
        for i in range(12):
            await self.guard.inspect_join(self.member(100 + i))
        self.channel.send.assert_awaited_once()

    async def test_disabled_config_ignores_joins(self):
        self.store.set_option(1, "raid_alert_config", {"enabled": False})
        await self.guard.inspect_join(self.member(100))
        self.assertEqual(self.guard.recent(1), [])
        self.channel.send.assert_not_awaited()

    async def test_different_guild_isolated(self):
        guild2 = NS(id=2, owner_id=6, me=NS(), get_channel=self.guild.get_channel)
        await self.guard.inspect_join(self.member(100, guild=guild2))
        self.assertEqual(self.guard.recent(1), [])
        self.assertEqual(self.guard.recent(2), [])

    async def test_setup_requires_owner(self):
        with self.assertRaises(ValueError):
            await self.guard.setup.callback(
                self.guard, self.interaction(actor_id=8), self.channel, 8, 60,
            )

    async def test_setup_and_test_alert(self):
        i = self.interaction()
        await self.guard.setup.callback(self.guard, i, self.channel, 8, 60)
        self.assertEqual(self.guard.settings(1)["threshold"], 8)
        await self.guard.test.callback(self.guard, i)
        self.channel.send.assert_awaited_once()

    async def test_persistent_settings_and_recent_joins(self):
        await self.guard.inspect_join(self.member(123))
        self.store.db.close()
        self.store = Store(str(Path(self.temp.name) / "store.sqlite3"))
        self.guard.store = self.store
        self.assertEqual(self.guard.recent(1)[0]["user"], 123)
        self.assertEqual(self.guard.settings(1)["window_seconds"], 60)

    async def test_disable_stops_monitoring(self):
        await self.guard.disable.callback(self.guard, self.interaction())
        await self.guard.inspect_join(self.member(123))
        self.assertEqual(self.guard.recent(1), [])
        self.channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
