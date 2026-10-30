import hashlib
import json
import re
import secrets
import time
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
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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
            CREATE TABLE IF NOT EXISTS options (
                guild INTEGER, key TEXT, value TEXT, PRIMARY KEY (guild, key)
            );
            CREATE TABLE IF NOT EXISTS networks (
                id TEXT PRIMARY KEY, home INTEGER UNIQUE, name TEXT
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                guild INTEGER PRIMARY KEY, network TEXT
            );
            CREATE TABLE IF NOT EXISTS invitations (
                hash TEXT PRIMARY KEY, kind TEXT, target TEXT, expires INTEGER
            );
            CREATE TABLE IF NOT EXISTS global_bans (
                network TEXT, member INTEGER, reason TEXT, actor INTEGER,
                PRIMARY KEY(network, member)
            );
            CREATE TABLE IF NOT EXISTS whitelist (
                guild INTEGER, member INTEGER, PRIMARY KEY(guild, member)
            );
            CREATE TABLE IF NOT EXISTS ban_results (
                network TEXT, member INTEGER, guild INTEGER, result TEXT,
                PRIMARY KEY(network, member, guild)
            );
            CREATE TABLE IF NOT EXISTS chat_channels (
                channel INTEGER PRIMARY KEY, guild INTEGER, room TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_snapshots (
                guild INTEGER, channel INTEGER, snapshot TEXT,
                PRIMARY KEY(guild, channel)
            );
            CREATE TABLE IF NOT EXISTS message_backups (
                guild INTEGER, channel INTEGER, message INTEGER, timestamp INTEGER,
                payload TEXT, PRIMARY KEY(guild, channel,message)
            );
        """)

    def option(self, guild: int, key: str, default=None):
        row = self.db.execute("SELECT value FROM options WHERE guild=? AND key=?", (guild, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set_option(self, guild: int, key: str, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO options VALUES (?,?,?)", (guild, key, json.dumps(value)))

    def network(self, guild: int):
        return self.db.execute("SELECT n.* FROM networks n JOIN subscriptions s ON s.network=n.id WHERE s.guild=?", (guild,)).fetchone()

    def create_network(self, guild: int, name: str):
        if self.network(guild):
            raise ValueError("This server already belongs to a ban network. Leave it first.")
        network = secrets.token_hex(12)
        with self.db:
            self.db.execute("INSERT INTO networks VALUES (?,?,?)", (network, guild, name))
            self.db.execute("INSERT INTO subscriptions VALUES (?,?)", (guild, network))
        return network

    def invite(self, kind: str, target: str):
        code = secrets.token_urlsafe(18)
        with self.db:
            self.db.execute("DELETE FROM invitations WHERE expires<=?", (int(time.time()),))
            self.db.execute("INSERT INTO invitations VALUES (?,?,?,?)", (hashlib.sha256(code.encode()).hexdigest(), kind, target, int(time.time()) + 3600))
        return code

    def consume_invite(self, kind: str, code: str):
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.db:
            row = self.db.execute("SELECT * FROM invitations WHERE hash=? AND kind=? AND expires>?", (digest, kind, int(time.time()))).fetchone()
            if not row:
                raise ValueError("That invitation is invalid, expired, or already used.")
            self.db.execute("DELETE FROM invitations WHERE hash=?", (digest,))
        return row["target"]

    def join_network(self, guild: int, code: str):
        if self.network(guild):
            raise ValueError("This server already belongs to a ban network.")
        network = self.consume_invite("network", code)
        if not self.db.execute("SELECT 1 FROM networks WHERE id=?", (network,)).fetchone():
            raise ValueError("That network no longer exists.")
        with self.db:
            self.db.execute("INSERT INTO subscriptions VALUES (?,?)", (guild, network))
        return network

    def leave_network(self, guild: int):
        network = self.network(guild)
        with self.db:
            if network and network["home"] == guild:
                self.db.execute("DELETE FROM subscriptions WHERE network=?", (network["id"],))
                self.db.execute("DELETE FROM global_bans WHERE network=?", (network["id"],))
                self.db.execute("DELETE FROM networks WHERE id=?", (network["id"],))
                self.db.execute("DELETE FROM ban_results WHERE network=?", (network["id"],))
            else:
                self.db.execute("DELETE FROM subscriptions WHERE guild=?", (guild,))

    def whitelisted(self, guild: int, member: int):
        return bool(self.db.execute("SELECT 1 FROM whitelist WHERE guild=? AND member=?", (guild, member)).fetchone())

    def set_whitelist(self, guild: int, member: int, enabled: bool):
        with self.db:
            if enabled:
                self.db.execute("INSERT OR IGNORE INTO whitelist VALUES (?,?)", (guild, member))
            else:
                self.db.execute("DELETE FROM whitelist WHERE guild=? AND member=?", (guild, member))

    def global_ban(self, guild: int, member: int):
        return self.db.execute("SELECT b.* FROM global_bans b JOIN subscriptions s ON s.network=b.network WHERE s.guild=? AND b.member=?", (guild, member)).fetchone()

    def snapshot(self, guild: int, channel: int, data: dict):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO channel_snapshots VALUES (?,?,?)", (guild, channel, json.dumps(data)))

    def snapshots(self, guild: int):
        return [(row[0], json.loads(row[1])) for row in self.db.execute("SELECT channel,snapshot FROM channel_snapshots WHERE guild=?", (guild,))]

    def archive(self, guild: int, channel: int, message: int, timestamp: int, payload: dict):
        with self.db:
            previous = self.db.execute("SELECT payload FROM message_backups WHERE guild=? AND channel=? AND message=?", (guild, channel, message)).fetchone()
            self.db.execute("INSERT OR REPLACE INTO message_backups VALUES (?,?,?,?,?)", (guild, channel, message, timestamp, json.dumps(payload)))
            evicted = self.db.execute("SELECT payload FROM message_backups WHERE guild=? AND channel=? AND message NOT IN (SELECT message FROM message_backups WHERE guild=? AND channel=? ORDER BY timestamp DESC,message DESC LIMIT 1000)", (guild, channel, guild, channel)).fetchall()
            self.db.execute("DELETE FROM message_backups WHERE guild=? AND channel=? AND message NOT IN (SELECT message FROM message_backups WHERE guild=? AND channel=? ORDER BY timestamp DESC,message DESC LIMIT 1000)", (guild, channel, guild, channel))
        return ([json.loads(previous[0])] if previous else []) + [json.loads(row[0]) for row in evicted]

    def messages(self, guild: int, channel: int):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM message_backups WHERE guild=? AND channel=? ORDER BY timestamp,message", (guild, channel))]

    def remap_channel(self, guild: int, old: int, new: int):
        with self.db:
            # Gateway CREATE can arrive before the REST creation response.
            self.db.execute("DELETE FROM channel_snapshots WHERE guild=? AND channel=?", (guild, new))
            self.db.execute("UPDATE channel_snapshots SET channel=? WHERE guild=? AND channel=?", (new, guild, old))
            self.db.execute("UPDATE message_backups SET channel=? WHERE guild=? AND channel=?", (new, guild, old))
            self.db.execute("UPDATE chat_channels SET channel=? WHERE guild=? AND channel=?", (new, guild, old))
            for key in ("deathnote_channel", "verification"):
                value = self.option(guild, key)
                if key == "deathnote_channel" and value == old:
                    self.db.execute("UPDATE options SET value=? WHERE guild=? AND key=?", (json.dumps(new), guild, key))
                elif isinstance(value, dict) and value.get("channel") == old:
                    value["channel"] = new
                    self.db.execute("UPDATE options SET value=? WHERE guild=? AND key=?", (json.dumps(value), guild, key))
            for channel, snapshot in self.snapshots(guild):
                if snapshot.get("category") == old:
                    snapshot["category"] = new
                    self.db.execute("UPDATE channel_snapshots SET snapshot=? WHERE guild=? AND channel=?", (json.dumps(snapshot), guild, channel))

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
