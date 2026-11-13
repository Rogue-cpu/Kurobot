"""Unit checks for the owner-controlled vanity guard (no live Discord calls)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from storage import Store
from vanity_guard import VanityGuard


class VanityGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "kuro.sqlite3"))
        self.bot = NS(user=NS(id=99), guilds=[], wait_until_ready=AsyncMock())
        self.guard = VanityGuard(self.bot, self.store)
        self.channel = NS(send=AsyncMock())
        self.guild = NS(
            id=1, owner_id=5, me=NS(
                guild_permissions=NS(view_audit_log=False, moderate_members=False)
            ), get_channel=Mock(return_value=self.channel)
        )
        self.store.set_option(1, "vanity_guard", {
            "enabled": True, "code": "original", "last_seen": "original",
            "channel": 10, "timeout_minutes": 0,
        })

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_unexpected_change_alerts_once_and_survives_restart(self):
        await self.guard.inspect(self.guild, "changed", "poll")
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.option(1, "vanity_guard")["code"], "original")
        self.assertEqual(self.store.option(1, "vanity_guard")["last_seen"], "changed")
        self.assertEqual(len(self.store.option(1, "vanity_incidents")), 1)
        await self.guard.inspect(self.guild, "changed", "poll")
        self.channel.send.assert_awaited_once()
        self.store.db.close()
        self.store = Store(str(Path(self.temp.name) / "kuro.sqlite3"))
        self.guard.store = self.store
        await self.guard.inspect(self.guild, "changed", "poll")
        self.channel.send.assert_awaited_once()

    async def test_restoration_clears_alert_state_without_changing_baseline(self):
        await self.guard.inspect(self.guild, "changed", "poll")
        await self.guard.inspect(self.guild, "original", "poll")
        self.assertEqual(self.store.option(1, "vanity_guard")["last_seen"], "original")
        self.assertEqual(self.store.option(1, "vanity_guard")["code"], "original")
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_disabling_stops_changes(self):
        config = self.store.option(1, "vanity_guard")
        config["enabled"] = False
        self.store.set_option(1, "vanity_guard", config)
        await self.guard.inspect(self.guild, "changed", "poll")
        self.channel.send.assert_not_called()

    async def test_unaudited_change_cannot_trigger_timeout(self):
        self.guard.timeout_actor = AsyncMock()
        self.guard.match_audit_actor = AsyncMock(return_value=None)
        await self.guard.inspect(self.guild, "changed", "poll")
        self.guard.timeout_actor.assert_not_called()
        self.guard.match_audit_actor.assert_not_called()

    async def test_confirmed_gateway_change_can_enforce_owner_opt_in(self):
        config = self.store.option(1, "vanity_guard")
        config["timeout_minutes"] = 10
        self.store.set_option(1, "vanity_guard", config)
        self.guard.match_audit_actor = AsyncMock(return_value=12)
        self.guard.timeout_actor = AsyncMock(return_value="timed out for 10 minutes")
        await self.guard.inspect(self.guild, "changed", "gateway")
        self.guard.timeout_actor.assert_awaited_once_with(self.guild, 12, 10)
        self.assertEqual(self.store.option(1, "vanity_incidents")[0]["actor"], 12)

    async def test_failed_alert_cannot_trigger_punishment(self):
        import discord
        config = self.store.option(1, "vanity_guard")
        config["timeout_minutes"] = 10
        self.store.set_option(1, "vanity_guard", config)
        self.guard.match_audit_actor = AsyncMock(return_value=12)
        self.guard.timeout_actor = AsyncMock()
        # Discord's HTTPException needs a response with status.
        response = NS(status=403, reason="Forbidden")
        self.channel.send.side_effect = discord.HTTPException(response, "Cannot send")
        await self.guard.inspect(self.guild, "changed", "gateway")
        self.guard.timeout_actor.assert_not_called()
        self.assertEqual(self.store.option(1, "vanity_guard")["last_seen"], "original")

    async def test_owner_cannot_be_timed_out(self):
        self.assertEqual(
            await self.guard.timeout_actor(self.guild, 5, 10),
            "protected owner or bot; no action",
        )

    async def test_audit_attribution_requires_both_diffs_to_match(self):
        self.guild.me.guild_permissions.view_audit_log = True
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc)
        entries = [
            NS(
                created_at=recent,
                before=NS(vanity_url_code="other"),
                after=NS(vanity_url_code="changed"),
                user=NS(id=7),
            ),
            NS(
                created_at=recent,
                before=NS(vanity_url_code="original"),
                after=NS(vanity_url_code="changed"),
                user=NS(id=8),
            ),
        ]
        async def audit_logs(**kwargs):
            for entry in entries:
                yield entry
        self.guild.audit_logs = audit_logs
        self.assertEqual(
            await self.guard.match_audit_actor(self.guild, "original", "changed"), 8
        )
