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

    def owner_interaction(self, owner_id=5):
        return NS(
            guild_id=self.guild.id,
            guild=self.guild,
            user=NS(id=owner_id),
            response=NS(send_message=AsyncMock()),
        )

    def set_preapproval(self, code="planned", expires_at=None):
        import time
        config = self.store.option(self.guild.id, "vanity_guard")
        config["preapproval"] = {
            "code": code, "baseline": "original",
            "expires_at": expires_at if expires_at is not None else int(time.time()) + 300,
            "approved_by": 5,
        }
        self.store.set_option(self.guild.id, "vanity_guard", config)

    async def test_owner_can_preapprove_one_exact_change(self):
        self.guard.current_code = AsyncMock(return_value="original")
        interaction = self.owner_interaction()
        await self.guard.preapprove.callback(self.guard, interaction, "planned", 10)
        approval = self.store.option(1, "vanity_guard")["preapproval"]
        self.assertEqual(approval["code"], "planned")
        self.assertEqual(approval["baseline"], "original")
        self.channel.send.assert_awaited_once()
        self.store.db.close()
        self.store = Store(str(Path(self.temp.name) / "kuro.sqlite3"))
        self.guard.store = self.store
        self.assertEqual(self.store.option(1, "vanity_guard")["preapproval"]["code"], "planned")
        self.assertFalse(self.store.option(2, "vanity_guard", {}).get("preapproval"))

    async def test_preapproval_requires_server_owner(self):
        self.guard.current_code = AsyncMock(return_value="original")
        with self.assertRaises(ValueError):
            await self.guard.preapprove.callback(
                self.guard, self.owner_interaction(owner_id=7), "planned", 10,
            )
        self.assertNotIn("preapproval", self.store.option(1, "vanity_guard"))

    async def test_owner_cannot_preapprove_after_live_code_changed(self):
        self.guard.current_code = AsyncMock(return_value="already-changed")
        with self.assertRaises(ValueError):
            await self.guard.preapprove.callback(
                self.guard, self.owner_interaction(), "planned", 10,
            )
        self.channel.send.assert_not_awaited()

    async def test_approved_change_skips_quarantine_and_updates_baseline(self):
        self.set_preapproval()
        config = self.store.option(1, "vanity_guard")
        config["response"] = "quarantine"
        self.store.set_option(1, "vanity_guard", config)
        self.guard.match_audit_actor = AsyncMock(return_value=12)
        self.guard.containment.quarantine = AsyncMock()
        await self.guard.inspect(self.guild, "planned", "gateway")
        new_config = self.store.option(1, "vanity_guard")
        self.assertEqual(new_config["code"], "planned")
        self.assertEqual(new_config["last_seen"], "planned")
        self.assertNotIn("preapproval", new_config)
        self.guard.match_audit_actor.assert_not_awaited()
        self.guard.containment.quarantine.assert_not_awaited()
        self.assertIn("owner preapproved", self.store.option(1, "vanity_incidents")[-1]["outcome"])

    async def test_unexpected_code_cancels_existing_preapproval_and_alerts(self):
        self.set_preapproval()
        self.guard.containment.quarantine = AsyncMock()
        await self.guard.inspect(self.guild, "not-the-planned-code", "poll")
        config = self.store.option(1, "vanity_guard")
        self.assertEqual(config["code"], "original")
        self.assertNotIn("preapproval", config)
        self.guard.containment.quarantine.assert_not_awaited()
        self.assertEqual(len(self.store.option(1, "vanity_incidents")), 1)
        self.channel.send.assert_awaited_once()

    async def test_expired_preapproval_does_not_bypass_alert(self):
        import time
        self.set_preapproval(expires_at=int(time.time()) - 1)
        await self.guard.inspect(self.guild, "planned", "poll")
        self.assertEqual(self.store.option(1, "vanity_guard")["code"], "original")
        self.assertNotIn("preapproval", self.store.option(1, "vanity_guard"))
        self.assertEqual(self.store.option(1, "vanity_incidents")[-1]["outcome"], "alert only")

    async def test_owner_can_cancel_preapproval(self):
        self.set_preapproval()
        i = self.owner_interaction()
        await self.guard.cancel_preapproval.callback(self.guard, i)
        self.assertNotIn("preapproval", self.store.option(1, "vanity_guard"))
        self.channel.send.assert_awaited_once()

    async def test_preapproval_alert_failure_does_not_authorize_change(self):
        import discord
        self.guard.current_code = AsyncMock(return_value="original")
        self.channel.send.side_effect = discord.HTTPException(
            NS(status=403, reason="Forbidden"), "Cannot send",
        )
        with self.assertRaises(ValueError):
            await self.guard.preapprove.callback(
                self.guard, self.owner_interaction(), "planned", 10,
            )
        self.assertNotIn("preapproval", self.store.option(1, "vanity_guard"))

    async def test_preapproval_notification_failure_keeps_old_baseline(self):
        import discord
        self.set_preapproval()
        self.channel.send.side_effect = discord.HTTPException(
            NS(status=403, reason="Forbidden"), "Cannot send",
        )
        await self.guard.inspect(self.guild, "planned", "gateway")
        self.assertEqual(self.store.option(1, "vanity_guard")["code"], "original")
        self.assertIn("preapproval", self.store.option(1, "vanity_guard"))

    async def test_quarantine_drill_does_not_change_any_role(self):
        target = NS(
            id=31, bot=False, roles=[], top_role=2,
            add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )
        self.guild.me.top_role = 10
        i = self.owner_interaction()
        await self.guard.drill.callback(self.guard, i, target)
        target.remove_roles.assert_not_awaited()
        target.add_roles.assert_not_awaited()
        self.assertIn("DRY RUN ONLY", i.response.send_message.await_args.args[0])

    async def test_owner_approving_during_audit_wait_cancels_stale_enforcement(self):
        config = self.store.option(1, "vanity_guard")
        config["response"] = "quarantine"
        self.store.set_option(1, "vanity_guard", config)
        async def audit_with_concurrent_owner_approval(guild, original, observed):
            current = self.store.option(guild.id, "vanity_guard")
            current["code"] = observed
            current["last_seen"] = observed
            self.store.set_option(guild.id, "vanity_guard", current)
            return 12
        self.guard.match_audit_actor = audit_with_concurrent_owner_approval
        self.guard.containment.quarantine = AsyncMock()
        await self.guard.inspect(self.guild, "changed", "gateway")
        self.guard.containment.quarantine.assert_not_awaited()

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

    async def test_privilege_grant_event_warns_owner(self):
        role = NS(id=44, permissions=NS(
            administrator=True, manage_guild=False, manage_roles=False,
        ))
        before = NS(guild=self.guild, roles=[])
        after = NS(guild=self.guild, id=123, roles=[role])
        self.guard.containment.uphold = AsyncMock(return_value=None)
        self.guard.warn_owner = AsyncMock()
        await self.guard.on_member_update(before, after)
        self.guard.warn_owner.assert_awaited_once()
        self.guard.containment.uphold.assert_awaited_once()

    async def test_role_permission_escalation_warns_owner(self):
        before = NS(permissions=NS(administrator=False, manage_guild=False))
        after = NS(
            guild=self.guild, id=44, name="Moderators",
            permissions=NS(administrator=False, manage_guild=True),
        )
        self.guard.warn_owner = AsyncMock()
        await self.guard.on_guild_role_update(before, after)
        self.guard.warn_owner.assert_awaited_once()

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
