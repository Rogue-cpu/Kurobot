import re
import sqlite3
from pathlib import Path


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip().lower())
    if not match:
        raise ValueError("Use a delay like 30s, 10m, 2h, or 3d.")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 365 * 86400:
        raise ValueError("Choose a delay of one year or less.")
    return seconds


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                guild INTEGER PRIMARY KEY, autorole INTEGER, sticky INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS member_roles (
                guild INTEGER, member INTEGER, role INTEGER,
                PRIMARY KEY (guild, member, role)
            );
            CREATE TABLE IF NOT EXISTS scheduled_bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, member INTEGER,
                due INTEGER, reason TEXT, actor INTEGER, status TEXT DEFAULT 'pending',
                error TEXT
            );
        """)

    def settings(self, guild: int):
        row = self.db.execute("SELECT * FROM settings WHERE guild=?", (guild,)).fetchone()
        return dict(row) if row else {"guild": guild, "autorole": None, "sticky": 0}

    def configure(self, guild: int, autorole: int | None, sticky: bool):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (guild, autorole, int(sticky)))

    def remember_roles(self, guild: int, member: int, roles: list[int]):
        with self.db:
            self.db.execute("DELETE FROM member_roles WHERE guild=? AND member=?", (guild, member))
            self.db.executemany("INSERT INTO member_roles VALUES (?, ?, ?)", [(guild, member, role) for role in roles])

    def roles(self, guild: int, member: int):
        return [row[0] for row in self.db.execute("SELECT role FROM member_roles WHERE guild=? AND member=?", (guild, member))]

    def schedule(self, guild: int, member: int, due: int, reason: str, actor: int):
        with self.db:
            cursor = self.db.execute("INSERT INTO scheduled_bans (guild,member,due,reason,actor) VALUES (?,?,?,?,?)", (guild, member, due, reason, actor))
        return cursor.lastrowid

    def pending(self, guild: int | None = None):
        if guild is None:
            return self.db.execute("SELECT * FROM scheduled_bans WHERE status='pending' ORDER BY due").fetchall()
        return self.db.execute("SELECT * FROM scheduled_bans WHERE status='pending' AND guild=? ORDER BY due", (guild,)).fetchall()

    def finish(self, job: int, status: str, error: str | None = None):
        with self.db:
            self.db.execute("UPDATE scheduled_bans SET status=?, error=? WHERE id=?", (status, error, job))

    def cancel(self, guild: int, job: int):
        with self.db:
            cursor = self.db.execute("UPDATE scheduled_bans SET status='cancelled' WHERE guild=? AND id=? AND status='pending'", (guild, job))
        return cursor.rowcount > 0
