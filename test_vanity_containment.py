"""Safety and persistence tests for vanity role quarantine."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from storage import Store
from vanity_containment import VanityContainment, dangerous_role


class Role:
    def __init__(self, role_id, rank, **permissions):
        self.id = role_id
        self.rank = rank
        self.permissions = discord.Permissions(**permissions)
        self.managed = False

    def is_default(self):
        return False

    def __lt__(self, other):
        return self.rank < other.rank

    def __ge__(self, other):
        return self.rank >= other.rank


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db"))
        self.bot = NS(user=NS(id=99))
        self.containment = VanityContainment(self.bot, self.store)
        self.admin = Role(12, 5, administrator=True)
        self.reader = Role(13, 4, read_messages=True)
        self.top = Role(98, 10)
        self.member = NS(id=15, bot=False, top_role=self.admin, roles=[self.admin, self.reader])
        async def drop(role, reason):
            self.member.roles.remove(role)
        async def assign(role, reason):
            self.member.roles.append(role)
        self.member.remove_roles = AsyncMock(side_effect=drop)
        self.member.add_roles = AsyncMock(side_effect=assign)
        self.guild = NS(
            id=1, owner_id=1,
            me=NS(top_role=self.top, guild_permissions=NS(manage_roles=True)),
            fetch_member=AsyncMock(return_value=self.member),
            get_role=lambda role_id: {12:self.admin, 13:self.reader}.get(role_id),
            roles=[self.admin, self.reader],
        )

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def test_dangerous_permission_classification(self):
        self.assertTrue(dangerous_role(self.admin))
        self.assertFalse(dangerous_role(self.reader))
        self.assertEqual(self.containment.risk_roles(self.guild), [self.admin])

    async def test_quarantine_only_removes_privileged_role(self):
        result = await self.containment.quarantine(self.guild, 15, "vanity change")
        self.assertIn("removed 1", result)
        self.assertEqual(self.member.roles, [self.reader])
        self.assertEqual(self.containment.record(1, 15)["roles"], [12])
        self.member.add_roles.assert_not_awaited()

    async def test_owners_never_quarantined(self):
        result = await self.containment.quarantine(self.guild, 1, "vanity change")
        self.assertIn("exempt", result)
        self.guild.fetch_member.assert_not_awaited()

    async def test_highest_role_boundary_enforced(self):
        self.member.top_role = Role(19, 11, administrator=True)
        result = await self.containment.quarantine(self.guild, 15, "vanity change")
        self.assertIn("above or equal", result)
        self.member.remove_roles.assert_not_awaited()

    async def test_role_records_survive_restart(self):
        await self.containment.quarantine(self.guild, 15, "vanity change")
        self.store.db.close()
        self.store = Store(str(Path(self.tmp.name) / "db"))
        self.containment.store = self.store
        self.assertEqual(self.containment.record(1, 15)["roles"], [12])

    async def test_restore_requires_explicit_caller_and_only_saved_roles(self):
        await self.containment.quarantine(self.guild, 15, "vanity change")
        result = await self.containment.restore(self.guild, 15)
        self.assertIn("Restored 1", result)
        self.assertIn(self.admin, self.member.roles)
        self.assertEqual(self.containment.record(1, 15)["roles"], [])
        self.member.add_roles.assert_awaited_once()

    async def test_quarantine_hold_removes_regained_dangerous_role(self):
        await self.containment.quarantine(self.guild, 15, "vanity change")
        self.member.roles.append(self.admin)
        result = await self.containment.uphold(self.guild, self.member, [self.admin])
        self.assertIn("removed 1", result)
        self.assertEqual(self.member.roles, [self.reader])
        self.assertEqual(self.containment.record(1, 15)["roles"], [12])

    async def test_non_quarantined_user_does_not_trigger_hold(self):
        result = await self.containment.uphold(self.guild, self.member, [self.admin])
        self.assertIsNone(result)
        self.member.remove_roles.assert_not_awaited()

    async def test_bot_accounts_are_not_automatically_modified(self):
        self.member.bot = True
        result = await self.containment.quarantine(self.guild, 15, "vanity change")
        self.assertIn("bot account", result)
        self.member.remove_roles.assert_not_awaited()

    async def test_partial_failure_keeps_clear_recovery_record(self):
        async def failure(role, reason):
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Denied")
        self.member.remove_roles.side_effect = failure
        result = await self.containment.quarantine(self.guild, 15, "vanity change")
        self.assertIn("owner attention", result)
        record = self.containment.record(1, 15)
        self.assertEqual(record["roles"], [])
        self.assertEqual(record["failed"], [12])
        self.assertEqual(record["status"], "partial")
