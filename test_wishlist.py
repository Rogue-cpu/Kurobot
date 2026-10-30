import asyncio
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock

import discord
from discord.ext import commands

from features import Features
from storage import Store
from wishlist import BlueprintConfirm,Wishlist,moderation_target
from wishlist_store import WishlistStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/"db")
        self.core=Store(self.path)
        self.state=WishlistStore(self.core)

    def tearDown(self):
        self.core.db.close()
        self.temp.cleanup()

    def test_xp_cooldown_duplicates_and_server_isolation(self):
        self.assertEqual(self.state.award(1,2,"A proper message",now=100),15)
        self.assertIsNone(self.state.award(1,2,"Different message",now=110))
        self.assertIsNone(self.state.award(1,2,"a  proper MESSAGE",now=200))
        self.assertEqual(self.state.award(1,2,"Another useful message",now=201),30)
        self.assertEqual(self.state.award(9,2,"A proper message",now=100),15)
        self.assertIsNone(self.state.award(1,3,"hi",now=100))
        self.core.db.close()
        self.core=Store(self.path)
        self.state=WishlistStore(self.core)
        self.assertIsNone(self.state.award(1,2,"After restart message",now=210))

    def test_level_boundaries(self):
        self.assertEqual([self.state.level(value) for value in (0,99,100,399,400,899,900)],[0,0,1,1,2,2,3])

    def test_whitelist_flags_do_not_silently_requeue_resolved_actions(self):
        self.state.flag(1,2,3,"rule")
        self.core.db.execute("UPDATE watch_flags SET status='cleared'")
        self.core.db.commit()
        self.state.flag(1,2,3,"rule again")
        self.assertEqual(self.core.db.execute("SELECT status FROM watch_flags").fetchone()[0],"cleared")

    def test_review_decisions_are_not_reset_by_repeated_challenges(self):
        self.assertEqual(self.state.review(1,2,["young"]),"pending")
        self.core.db.execute("UPDATE reviews SET status='rejected'")
        self.core.db.commit()
        self.assertEqual(self.state.review(1,2,["new flag"]),"rejected")
        self.assertIsNone(self.core.db.execute("SELECT 1 FROM reviews WHERE guild=9").fetchone())

    def test_role_recovery_updates_channel_access_and_role_settings(self):
        self.core.snapshot(1,10,{"overwrites":[{"id":20,"role":True,"allow":0,"deny":1024}]})
        self.core.configure(1,20,True)
        self.core.set_option(1,"verification",{"role":20,"channel":10})
        self.state.entity(1,"role",20,{"name":"Member"})
        self.core.db.execute("INSERT INTO rewards VALUES (1,2,20)")
        self.core.db.commit()
        self.state.remap_role(1,20,30)
        self.assertEqual(self.core.settings(1)["autorole"],30)
        self.assertEqual(self.core.option(1,"verification")["role"],30)
        self.assertEqual(dict(self.core.snapshots(1))[10]["overwrites"][0]["id"],30)
        self.assertEqual(self.core.db.execute("SELECT role FROM rewards").fetchone()[0],30)

    def test_extended_backup_limit(self):
        self.core.set_option(1,"recovery_plus",{"limit":1100})
        for index in range(1101):
            self.core.archive(1,2,index,index,{"id":index})
        self.assertEqual(len(self.core.messages(1,2)),1100)
        self.assertEqual(self.core.messages(1,2)[0]["id"],1)

    def test_jail_and_channel_layout_survive_restart(self):
        self.core.db.execute("INSERT INTO jails VALUES (1,2,3,9999,4,1,'active','reason')")
        self.core.db.commit()
        self.core.snapshot(1,20,{"type":0,"category":10,"position":3})
        self.core.db.close()
        self.core=Store(self.path)
        self.state=WishlistStore(self.core)
        self.assertEqual(self.state.jail(1,2)["due"],9999)
        self.assertEqual(dict(self.core.snapshots(1))[20]["category"],10)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=10
        self.temp=tempfile.TemporaryDirectory()
        self.core=Store(str(Path(self.temp.name)/"db"))
        self.bot=NS(data_dir=Path(self.temp.name),user=NS(id=99),intents=NS(message_content=True),get_guild=Mock(),get_cog=Mock(return_value=None))
        self.features=Features(self.bot,self.core,lambda role,guild:True)
        self.cog=Wishlist(self.bot,self.core,self.features)

    async def asyncTearDown(self):
        self.core.db.close()
        self.temp.cleanup()

    async def test_jail_release_preserves_timeout_changed_by_another_moderator(self):
        role=object()
        member=NS(timed_out_until=discord.utils.utcnow()+timedelta(days=2),roles=[role],timeout=AsyncMock(),remove_roles=AsyncMock())
        guild=NS(id=1,fetch_member=AsyncMock(return_value=member),get_role=Mock(return_value=role))
        row={"member":2,"role":3,"due":int(time.time())+60,"label_added":1}
        await self.cog.release_jail(guild,row,manual=True)
        member.timeout.assert_not_called()
        member.remove_roles.assert_awaited_once()

    async def test_jail_rejects_owner_and_administrator(self):
        guild=NS(owner_id=1,me=NS(id=99,top_role=10))
        actor=NS(id=2,top_role=5)
        for target in (NS(id=1,bot=False),NS(id=3,bot=False,top_role=1,guild_permissions=NS(administrator=True))):
            with self.assertRaises(ValueError):
                moderation_target(guild,actor,target)

    async def test_watch_whitelist_skips_all_membership_and_ban_calls(self):
        self.core.db.execute("INSERT INTO watches VALUES (1,2,'ban','reason',3)")
        self.core.db.commit()
        guild=NS(id=1,owner_id=3,ban=AsyncMock())
        source=NS(fetch_member=AsyncMock())
        self.bot.get_guild.side_effect=lambda value: {1:guild,2:source}[value]
        self.core.set_whitelist(1,4,True)
        self.cog.state.flag(1,4,2,"reason")
        row=self.core.db.execute("SELECT * FROM watch_flags").fetchone()
        await self.cog.process_watch(row)
        source.fetch_member.assert_not_called()
        guild.ban.assert_not_called()
        self.assertEqual(self.core.db.execute("SELECT status FROM watch_flags").fetchone()[0],"whitelisted")

    async def test_verification_policy_creates_review_without_granting_role(self):
        self.core.set_option(1,"verification_policy",{"enabled":True,"avatar":True,"burst":0})
        self.core.set_option(1,"verification",{"min_days":7})
        member=NS(id=2,guild=NS(id=1),created_at=discord.utils.utcnow(),avatar=None)
        result=await self.cog.verification_gate(member)
        self.assertIn("waiting",result)
        reasons=json.loads(self.core.db.execute("SELECT reasons FROM reviews").fetchone()[0])
        self.assertEqual(len(reasons),2)

    async def test_active_jail_blocks_previously_approved_verification(self):
        self.core.db.execute("INSERT INTO jails VALUES (1,2,3,9999999999,4,1,'active','reason')")
        self.core.db.commit()
        result=await self.cog.verification_gate(NS(id=2,guild=NS(id=1)))
        self.assertIn("active jail",result)

    async def test_private_threads_are_never_copied(self):
        self.core.set_option(1,"recovery_plus",{"threads":True})
        self.cog.state.entity(1,"thread",10,{"private":True})
        guild=NS(id=1,fetch_channel=AsyncMock())
        await self.cog.restore_threads(guild)
        guild.fetch_channel.assert_not_called()

    async def test_blueprint_needs_current_owner_and_preserves_invitation(self):
        code=self.core.invite("blueprint","key")
        view=BlueprintConfirm(self.cog,code,3)
        interaction=NS(user=NS(id=4),guild=NS(owner_id=3),response=NS(send_message=AsyncMock()))
        await view.confirm.callback(interaction)
        self.assertFalse(view.used)
        self.assertEqual(self.core.consume_invite("blueprint",code),"key")

    async def test_blueprint_queues_only_after_owner_confirmation(self):
        payload={"source":1,"name":"Source","roles":[],"channels":[]}
        self.core.db.execute("INSERT INTO blueprints VALUES ('key',?)",(json.dumps(payload),))
        self.core.db.commit()
        code=self.core.invite("blueprint","key")
        view=BlueprintConfirm(self.cog,code,3)
        interaction=NS(user=NS(id=3),guild_id=2,guild=NS(owner_id=3),response=NS(edit_message=AsyncMock()))
        await view.confirm.callback(interaction)
        self.assertTrue(view.used)
        row=self.core.db.execute("SELECT * FROM blueprint_jobs").fetchone()
        self.assertEqual(row["status"],"queued")
        self.assertEqual(json.loads(row["payload"])["role_map"],{"1":2})

    async def test_blueprint_stops_if_ownership_changes(self):
        guild=NS(owner_id=8,create_role=AsyncMock())
        self.bot.get_guild.return_value=guild
        self.core.db.execute("INSERT INTO blueprint_jobs(guild,actor,payload,status) VALUES (1,2,'{}','queued')")
        self.core.db.commit()
        row=self.core.db.execute("SELECT * FROM blueprint_jobs").fetchone()
        await self.cog.blueprint_step(row)
        guild.create_role.assert_not_called()
        self.assertEqual(self.core.db.execute("SELECT status FROM blueprint_jobs").fetchone()[0],"failed")

    async def test_category_delete_queues_and_does_not_create_in_handler(self):
        self.core.set_option(1,"recovery",{"enabled":True})
        self.core.snapshot(1,10,{"type":4,"name":"Games","position":0})
        channel=NS(id=10,guild=NS(id=1))
        self.features.reconcile=AsyncMock()
        await self.features.on_guild_channel_delete(channel)
        self.features.reconcile.assert_not_called()
        self.assertEqual(self.core.db.execute("SELECT status FROM recovery_jobs").fetchone()[0],"pending")

    async def test_nuked_layout_heals_one_step_at_a_time_categories_before_children(self):
        self.core.set_option(1,"recovery",{"enabled":True})
        for channel_id,snapshot in ((10,{"type":4,"name":"Games","position":0,"category":None}), (20,{"type":0,"name":"chat","category":10,"position":1}), (30,{"type":4,"name":"Art","position":2,"category":None}), (40,{"type":0,"name":"sketches","category":30,"position":3})):
            self.core.snapshot(1,channel_id,snapshot)
        live={}
        guild=NS(id=1,fetch_channels=AsyncMock(side_effect=lambda:list(live.values())),get_channel=lambda value:live.get(value))
        restored=[]
        async def restore(guild,old,snapshot):
            created=NS(id=old+1000,category_id=snapshot.get("category"))
            self.core.remap_channel(1,old,created.id)
            live[created.id]=created
            restored.append((old,snapshot.get("category")))
            return created
        self.features.restore_channel=AsyncMock(side_effect=restore)
        for _ in range(4):
            before=len(restored)
            await self.features.reconcile(guild)
            self.assertEqual(len(restored),before+1)
        self.assertEqual(restored,[(10,None),(30,None),(20,1010),(40,1030)])
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM recovery_jobs WHERE status='restored'").fetchone()[0],4)

    async def test_recovery_failure_is_persisted_and_backed_off(self):
        self.core.set_option(1,"recovery",{"enabled":True})
        self.core.snapshot(1,10,{"type":0,"name":"chat","position":0,"category":None})
        guild=NS(id=1,fetch_channels=AsyncMock(return_value=[]))
        self.features.restore_channel=AsyncMock(side_effect=ValueError("missing permission role"))
        await self.features.reconcile(guild)
        await self.features.reconcile(guild)
        self.features.restore_channel.assert_awaited_once()
        row=self.core.db.execute("SELECT * FROM recovery_jobs").fetchone()
        self.assertEqual(row["status"],"waiting")
        self.assertGreater(row["retry_at"],time.time())

    async def test_created_and_moved_channels_keep_their_current_category(self):
        self.core.set_option(1,"recovery",{"enabled":True})
        channel=NS(id=20,guild=NS(id=1))
        self.features.channel_snapshot=Mock(return_value={"name":"chat","type":0,"position":2,"category":10})
        await self.features.on_guild_channel_create(channel)
        self.assertEqual(dict(self.core.snapshots(1))[20]["category"],10)
        self.features.channel_snapshot.return_value["category"]=30
        before=NS(category_id=10)
        channel.category_id=30
        await self.features.on_guild_channel_update(before,channel)
        self.assertEqual(dict(self.core.snapshots(1))[20]["category"],30)

    async def test_recovery_queue_survives_reopening_database(self):
        self.core.set_option(1,"recovery",{"enabled":True})
        self.core.snapshot(1,20,{"name":"chat","type":0,"position":2,"category":10})
        await self.features.on_guild_channel_delete(NS(id=20,guild=NS(id=1)))
        self.core.db.close()
        self.core=Store(str(Path(self.temp.name)/"db"))
        self.assertEqual(self.core.db.execute("SELECT status FROM recovery_jobs").fetchone()[0],"pending")
        self.assertEqual(dict(self.core.snapshots(1))[20]["category"],10)

    async def test_unlock_preserves_later_manual_permission_edit(self):
        self.core.db.execute("INSERT INTO lock_states VALUES (1,2,3,'role',?,?)",(json.dumps({"send_messages":None,"connect":None}),json.dumps({"send_messages":False,"connect":False})))
        self.core.db.commit()
        overwrite=discord.PermissionOverwrite(send_messages=True,connect=False)
        channel=NS(overwrites_for=Mock(return_value=overwrite),set_permissions=AsyncMock())
        guild=NS(owner_id=4,get_channel=Mock(return_value=channel),get_role=Mock(return_value=object()))
        interaction=NS(guild_id=1,guild=guild,user=NS(id=4),response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        await self.cog.unlock.callback(self.cog,interaction)
        applied=channel.set_permissions.await_args.kwargs["overwrite"]
        self.assertTrue(applied.send_messages)
        self.assertIsNone(applied.connect)

    async def test_blueprint_private_default_does_not_become_public(self):
        payload={"roles":[],"channels":[[10,{"type":0,"name":"private","position":0,"category":None,"default_view":False,"overwrites":[]}]],"role_step":0,"channel_step":0,"role_map":{"9":1},"channel_map":{}}
        self.core.db.execute("INSERT INTO blueprint_jobs(guild,actor,payload,status) VALUES (1,2,?,'queued')",(json.dumps(payload),))
        self.core.db.commit()
        self.bot.get_guild.return_value=NS(id=1,owner_id=2)
        self.features.restore_channel=AsyncMock(return_value=NS(id=50))
        await self.cog.blueprint_step(self.core.db.execute("SELECT * FROM blueprint_jobs").fetchone())
        data=self.features.restore_channel.await_args.args[2]
        self.assertTrue(data["overwrites"][0]["deny"] & discord.Permissions.view_channel.flag)

    async def test_blueprint_member_overwrites_stop_for_review(self):
        payload={"roles":[],"channels":[[10,{"type":0,"name":"private","position":0,"category":None,"overwrites":[{"id":40,"role":False,"allow":1024,"deny":0}]}]],"role_step":0,"channel_step":0,"role_map":{"9":1},"channel_map":{}}
        self.core.db.execute("INSERT INTO blueprint_jobs(guild,actor,payload,status) VALUES (1,2,?,'queued')",(json.dumps(payload),))
        self.core.db.commit()
        self.bot.get_guild.return_value=NS(id=1,owner_id=2)
        self.features.restore_channel=AsyncMock()
        await self.cog.blueprint_step(self.core.db.execute("SELECT * FROM blueprint_jobs").fetchone())
        self.features.restore_channel.assert_not_called()
        self.assertEqual(self.core.db.execute("SELECT status FROM blueprint_jobs").fetchone()[0],"failed")

    async def test_all_groups_serialize_for_discord(self):
        bot=commands.Bot(command_prefix="!",intents=discord.Intents.none())
        bot.data_dir=Path(self.temp.name)
        async with bot:
            features=Features(bot,self.core,lambda role,guild:True)
            await bot.add_cog(features)
            await bot.add_cog(Wishlist(bot,self.core,features))
            names={command.name for command in bot.tree.get_commands()}
            self.assertTrue({"jail","levels","watch","blueprint","protection","recovery_plus","verification_policy","selfrole"}<=names)
            for command in bot.tree.get_commands():
                command.to_dict(bot.tree)


if __name__=="__main__":
    unittest.main()
