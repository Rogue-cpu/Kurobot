import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import zipfile

from backup_commands import Backups
from recovery_backup import create_bundle,verify_bundle,restore_bundle,resume_restored
from storage import Store


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.data=self.root/"data"
        self.core=Store(str(self.data/"kuro.sqlite3"))
        self.core.configure(1,22,True)
        (self.data/"attachments").mkdir()
        (self.data/"attachments"/"100-200").write_bytes(b"saved attachment")
        (self.data/".env").write_text("NOT_A_REAL_SECRET=must_not_be_exported")
        self.core.archive(1,2,100,100,{"id":100,"content":"saved message","attachments":[{"file":"100-200","name":"hello.txt"}]})
        self.bundle=self.root/"recovery.zip"

    def tearDown(self):
        self.core.db.close()
        self.temp.cleanup()

    def test_live_wal_database_and_referenced_attachments_round_trip(self):
        result=create_bundle(self.data,self.bundle)
        self.assertEqual(set(result["files"]),{"kuro.sqlite3","attachments/100-200"})
        self.assertEqual(verify_bundle(self.bundle)["version"],1)
        restored=self.root/"standby"
        restore_bundle(self.bundle,restored)
        db=sqlite3.connect(restored/"kuro.sqlite3")
        self.assertEqual(db.execute("SELECT autorole FROM settings WHERE guild=1").fetchone()[0],22)
        self.assertEqual(db.execute("SELECT value FROM options WHERE key='restore_paused'").fetchone()[0],"true")
        db.close()
        self.assertEqual((restored/"attachments"/"100-200").read_bytes(),b"saved attachment")
        self.assertFalse((restored/".env").exists())

    def test_unreferenced_files_secrets_and_symlink_escape_are_not_exported(self):
        (self.data/"attachments"/"999-999").write_bytes(b"not referenced")
        self.core.archive(1,2,101,101,{"id":101,"attachments":[{"file":"../.env"}]})
        create_bundle(self.data,self.bundle)
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertEqual(set(archive.namelist()),{"manifest.json","kuro.sqlite3","attachments/100-200"})

    def test_missing_attachments_are_reported_instead_of_claimed_saved(self):
        (self.data/"attachments"/"100-200").unlink()
        result=create_bundle(self.data,self.bundle)
        self.assertEqual(result["missing_attachments"],["attachments/100-200"])
        self.assertNotIn("attachments/100-200",result["files"])

    def test_bundle_creation_and_restore_never_overwrite_existing_paths(self):
        self.bundle.write_bytes(b"keep existing file")
        with self.assertRaises(ValueError):
            create_bundle(self.data,self.bundle)
        self.assertEqual(self.bundle.read_bytes(),b"keep existing file")
        self.bundle.unlink()
        create_bundle(self.data,self.bundle)
        with self.assertRaises(FileExistsError):
            restore_bundle(self.bundle,self.data)
        self.assertEqual(self.core.settings(1)["autorole"],22)

    def test_changed_payload_fails_checksum(self):
        create_bundle(self.data,self.bundle)
        corrupt=self.root/"corrupt.zip"
        with zipfile.ZipFile(self.bundle) as original,zipfile.ZipFile(corrupt,"w") as changed:
            for item in original.infolist():
                value=original.read(item.filename)
                if item.filename=="attachments/100-200":
                    value=b"wrong attachment"
                changed.writestr(item.filename,value)
        with self.assertRaises(ValueError):
            verify_bundle(corrupt)

    def test_path_traversal_is_rejected_before_restore(self):
        create_bundle(self.data,self.bundle)
        corrupt=self.root/"unsafe.zip"
        with zipfile.ZipFile(self.bundle) as original,zipfile.ZipFile(corrupt,"w") as changed:
            manifest=json.loads(original.read("manifest.json"))
            manifest["files"]["../escape"]={"size":1,"sha256":"wrong"}
            for item in original.infolist():
                if item.filename!="manifest.json":
                    changed.writestr(item.filename,original.read(item.filename))
            changed.writestr("../escape",b"x")
            changed.writestr("manifest.json",json.dumps(manifest))
        with self.assertRaises(ValueError):
            restore_bundle(corrupt,self.root/"unsafe-destination")
        self.assertFalse((self.root/"escape").exists())
        self.assertFalse((self.root/"unsafe-destination").exists())

    def test_restored_bot_stays_paused_until_operator_acknowledges(self):
        create_bundle(self.data,self.bundle)
        restored=self.root/"standby"
        restore_bundle(self.bundle,restored)
        environment=os.environ.copy()
        environment["KURO_DATA_DIR"]=str(restored)
        environment["DISCORD_TOKEN"]=""
        result=subprocess.run([sys.executable,"-c","import bot"],env=environment,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("Restored data is paused",result.stderr)
        resume_restored(restored)
        db=sqlite3.connect(restored/"kuro.sqlite3")
        self.assertIsNone(db.execute("SELECT value FROM options WHERE key='restore_paused'").fetchone())
        db.close()

    def test_incomplete_restore_cannot_be_resumed(self):
        restored=self.root/"partial"
        restored.mkdir()
        (restored/"RESTORE_INCOMPLETE").write_text("incomplete")
        with self.assertRaises(ValueError):
            resume_restored(restored)


class BackupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=10
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.core=Store(str(self.root/"kuro.sqlite3"))
        self.bot=NS(data_dir=self.root,is_owner=AsyncMock(return_value=False))
        self.cog=Backups(self.bot,self.core)

    async def asyncTearDown(self):
        self.core.db.close()
        self.temp.cleanup()

    async def test_server_owner_cannot_access_an_entire_multi_server_backup(self):
        interaction=NS(user=NS(id=1),response=NS(send_message=AsyncMock()))
        with self.assertRaises(ValueError):
            await self.cog.status.callback(self.cog,interaction)
        interaction.response.send_message.assert_not_called()

    async def test_scheduled_backups_stay_off_by_default(self):
        self.cog.create=AsyncMock()
        await self.cog.timer.coro(self.cog)
        self.cog.create.assert_not_called()

    async def test_retention_only_prunes_previously_recorded_generated_bundles(self):
        self.core.set_option(0,"backup_policy",{"keep":1})
        directory=self.root/"backups"
        directory.mkdir()
        unrelated=directory/"my-manual-backup.zip"
        unrelated.write_bytes(b"keep me")
        first=await self.cog.create()
        second=await self.cog.create()
        self.assertFalse((directory/first["name"]).exists())
        self.assertTrue((directory/second["name"]).exists())
        self.assertEqual(unrelated.read_bytes(),b"keep me")


if __name__=="__main__":
    unittest.main()
