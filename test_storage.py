import tempfile
import unittest
from pathlib import Path

from storage import Store, duration_seconds


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "kuro.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_duration_limits(self):
        self.assertEqual(duration_seconds("2h"), 7200)
        for invalid in ("0s", "-1m", "tomorrow", "366d", "1.5h"):
            with self.assertRaises(ValueError):
                duration_seconds(invalid)

    def test_schedule_survives_restart_and_is_ordered(self):
        self.store.schedule(1, 2, 200, "later", 3)
        self.store.schedule(1, 4, 100, "earlier", 3)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual([row["due"] for row in self.store.pending()], [100, 200])

    def test_cancellation_is_scoped_to_server(self):
        job = self.store.schedule(1, 2, 100, "reason", 3)
        self.assertFalse(self.store.cancel(9, job))
        self.assertTrue(self.store.cancel(1, job))
        self.assertFalse(self.store.cancel(1, job))
        self.assertEqual(self.store.pending(), [])

    def test_finished_jobs_are_not_retried(self):
        job = self.store.schedule(1, 2, 100, "reason", 3)
        self.store.finish(job, "failed", "missing permissions")
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.db.execute("SELECT error FROM scheduled_bans").fetchone()[0], "missing permissions")

    def test_roles_are_replaced_and_isolated(self):
        self.store.remember_roles(1, 2, [10, 11])
        self.store.remember_roles(1, 2, [12])
        self.assertEqual(self.store.roles(1, 2), [12])
        self.assertEqual(self.store.roles(9, 2), [])

    def test_settings_survive_restart(self):
        self.store.configure(1, 22, True)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.settings(1)["autorole"], 22)
        self.assertEqual(self.store.settings(1)["sticky"], 1)
        self.assertIsNone(self.store.settings(9)["autorole"])


if __name__ == "__main__":
    unittest.main()
