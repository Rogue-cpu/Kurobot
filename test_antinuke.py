"""Non-destructive tests for anti-nuke audit-log signal monitoring."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord

from antinuke import AntiNuke
from storage import Store


class AntiNukeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "store.sqlite3"))
        self.bot = NS(user=NS(id=99))
        self.guard = AntiNuke(self.bot, self.store)
        self.channel = NS(
            id=10,
            mention="<#10>",
            send=AsyncMock(),
            permissions_for=Mock(return_value=NS(view_channel=True, send_messages=True)),
        )
        self.guild = NS(
            id=1, owner_id=5,
            me=NS(guild_permissions=NS(view_audit_log=True)),
            get_channel=Mock(return_value=self.channel),
        )
        self.store.set_option(1, "antinuke_config", {
            "enabled": True, "channel_id": 10,
            "threshold": 3, "window_seconds": 60,
        })

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def event(self, action=discord.AuditLogAction.channel_delete, actor=42,
              entry_id=100, guild=None):
        return NS(
            guild=guild or self.guild,
            id=entry_id,
            action=action,
            user_id=actor,
            target=NS(id=80 + entry_id),
        )

    def owner_interaction(self, owner_id=5):
        return NS(
            guild_id=self.guild.id,
            guild=self.guild,
            user=NS(id=owner_id),
            response=NS(send_message=AsyncMock()),
        )

    async def test_disabled_by_default(self):
        self.store.set_option(1, "antinuke_config", {"enabled": False})
        await self.guard.inspect_entry(self.event())
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.guard.events(1), [])

    async def test_threshold_alerts_only_after_three_independent_events(self):
        await self.guard.inspect_entry(self.event(entry_id=100))
        await self.guard.inspect_entry(self.event(entry_id=101, action=discord.AuditLogAction.role_delete))
        self.channel.send.assert_not_awaited()
        await self.guard.inspect_entry(self.event(entry_id=102, action=discord.AuditLogAction.kick))
        self.channel.send.assert_awaited_once()
        msg = self.channel.send.await_args.args[0]
        self.assertIn("Actor ID: 42", msg)
        self.assertIn("alert only", msg)
        self.assertEqual(len(self.guard.events(1)), 3)

    async def test_duplicate_entry_does_not_increment_threshold(self):
        for _ in range(4):
            await self.guard.inspect_entry(self.event(entry_id=100))
        self.assertEqual(len(self.guard.events(1)), 1)
        self.channel.send.assert_not_awaited()

    async def test_different_actors_do_not_combine_for_threshold(self):
        await self.guard.inspect_entry(self.event(actor=42, entry_id=100))
        await self.guard.inspect_entry(self.event(actor=43, entry_id=101))
        await self.guard.inspect_entry(self.event(actor=44, entry_id=102))
        self.channel.send.assert_not_awaited()

    async def test_bot_addition_alerts_immediately(self):
        await self.guard.inspect_entry(self.event(
            action=discord.AuditLogAction.bot_add, entry_id=150,
        ))
        self.channel.send.assert_awaited_once()
        self.assertIn("bot added", self.channel.send.await_args.args[0])

    async def test_unknown_actor_sensitive_event_alerts_without_attribution(self):
        await self.guard.inspect_entry(self.event(
            action=discord.AuditLogAction.webhook_create, actor=None,
        ))
        self.channel.send.assert_awaited_once()
        self.assertIn("unavailable", self.channel.send.await_args.args[0])

    async def test_never_alerts_for_bot_own_actions(self):
        await self.guard.inspect_entry(self.event(actor=99))
        self.assertEqual(self.guard.events(1), [])
        self.channel.send.assert_not_awaited()

    async def test_no_unverified_punishment_or_any_changes(self):
        self.guild.kick = AsyncMock()
        self.guild.ban = AsyncMock()
        self.guild.edit = AsyncMock()
        await self.guard.inspect_entry(self.event(action=discord.AuditLogAction.bot_add))
        self.guild.kick.assert_not_awaited()
        self.guild.ban.assert_not_awaited()
        self.guild.edit.assert_not_awaited()

    async def test_alert_cooldown_prevents_repeat_notification(self):
        for index in range(9):
            await self.guard.inspect_entry(self.event(entry_id=100 + index))
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.guard.events(1)), 9)

    async def test_missing_permission_fails_closed(self):
        self.guild.me.guild_permissions.view_audit_log = False
        await self.guard.inspect_entry(self.event(action=discord.AuditLogAction.bot_add))
        self.channel.send.assert_not_awaited()

    async def test_settings_are_per_server_and_persistent(self):
        await self.guard.inspect_entry(self.event(entry_id=100))
        self.store.db.close()
        self.store = Store(str(Path(self.temp.name) / "store.sqlite3"))
        self.guard.store = self.store
        self.assertEqual(len(self.guard.events(1)), 1)
        self.assertEqual(len(self.guard.events(2)), 0)

    async def test_setup_requires_server_owner(self):
        interaction = self.owner_interaction(owner_id=6)
        with self.assertRaises(ValueError):
            await self.guard.setup.callback(
                self.guard, interaction, self.channel, 4, 60,
            )

    async def test_setup_and_test_delivery(self):
        interaction = self.owner_interaction()
        await self.guard.setup.callback(
            self.guard, interaction, self.channel, 4, 60,
        )
        self.assertTrue(self.guard.settings(1)["enabled"])
        await self.guard.test.callback(self.guard, interaction)
        self.channel.send.assert_awaited_once()
        self.assertIn("TEST", self.channel.send.await_args.args[0])

    async def test_disable_stops_future_monitoring(self):
        await self.guard.disable.callback(self.guard, self.owner_interaction())
        await self.guard.inspect_entry(self.event(action=discord.AuditLogAction.bot_add))
        self.assertEqual(self.guard.events(1), [])
        self.channel.send.assert_not_awaited()

    async def test_event_history_remains_bounded(self):
        for entry_id in range(1000, 1230):
            await self.guard.inspect_entry(self.event(entry_id=entry_id, actor=42))
        self.assertEqual(len(self.guard.events(1)), 200)

    async def test_alert_failure_does_not_claim_delivery(self):
        self.channel.send.side_effect = discord.HTTPException(
            NS(status=403, reason="Forbidden"), "No access",
        )
        await self.guard.inspect_entry(self.event(action=discord.AuditLogAction.bot_add))
        self.assertEqual(len(self.guard.events(1)), 1)
        self.assertEqual(
            self.store.option(1, "antinuke_last_alert", {}), {},
        )


if __name__ == "__main__":
    unittest.main()
