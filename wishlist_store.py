"""Persistent state for moderation, rewards, recovery, and copy jobs."""
import hashlib
import json
import math
import time


class WishlistStore:
    def __init__(self, core):
        self.core, self.db = core, core.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jails (
                guild INTEGER, member INTEGER, role INTEGER, due INTEGER,
                actor INTEGER, label_added INTEGER DEFAULT 0, status TEXT, reason TEXT,
                PRIMARY KEY(guild,member)
            );
            CREATE TABLE IF NOT EXISTS scores (
                guild INTEGER, member INTEGER, xp INTEGER, last INTEGER, fingerprint TEXT,
                PRIMARY KEY(guild,member)
            );
            CREATE TABLE IF NOT EXISTS rewards (
                guild INTEGER, level INTEGER, role INTEGER, PRIMARY KEY(guild,level)
            );
            CREATE TABLE IF NOT EXISTS watches (
                guild INTEGER, source INTEGER, action TEXT, reason TEXT, actor INTEGER,
                PRIMARY KEY(guild,source)
            );
            CREATE TABLE IF NOT EXISTS watch_flags (
                guild INTEGER, member INTEGER, source INTEGER, status TEXT, detail TEXT, updated INTEGER,
                PRIMARY KEY(guild,member,source)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                guild INTEGER, member INTEGER, reasons TEXT, status TEXT,
                PRIMARY KEY(guild,member)
            );
            CREATE TABLE IF NOT EXISTS recent_joins (
                guild INTEGER, member INTEGER, joined INTEGER, PRIMARY KEY(guild,member)
            );
            CREATE TABLE IF NOT EXISTS entity_snapshots (
                guild INTEGER, kind TEXT, entity INTEGER, payload TEXT,
                PRIMARY KEY(guild,kind,entity)
            );
            CREATE TABLE IF NOT EXISTS blueprints (id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS blueprint_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, actor INTEGER,
                payload TEXT, status TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS lock_states (
                guild INTEGER, channel INTEGER, target INTEGER, kind TEXT,
                original TEXT, applied TEXT, PRIMARY KEY(guild,channel,target)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, member INTEGER,
                actor INTEGER, reason TEXT, created INTEGER
            );
            CREATE TABLE IF NOT EXISTS stickies (
                guild INTEGER, channel INTEGER PRIMARY KEY, content TEXT, message INTEGER, sent INTEGER
            );
            CREATE TABLE IF NOT EXISTS selfroles (guild INTEGER, role INTEGER, PRIMARY KEY(guild,role));
            CREATE TABLE IF NOT EXISTS recovered_role_members (
                guild INTEGER, role INTEGER, member INTEGER, status TEXT DEFAULT 'pending',
                PRIMARY KEY(guild,role,member)
            );
            CREATE TABLE IF NOT EXISTS recovered_staff_roles (
                guild INTEGER, role INTEGER, former_members TEXT, PRIMARY KEY(guild,role)
            );
        """)

    @staticmethod
    def level(xp):
        return math.isqrt(max(0, xp) // 100)

    def award(self, guild, member, content, amount=15, cooldown=60, now=None):
        now = int(time.time()) if now is None else now
        normalized = " ".join(content.casefold().split())
        if len(normalized) < 8:
            return None
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        row = self.db.execute("SELECT * FROM scores WHERE guild=? AND member=?", (guild, member)).fetchone()
        if row and (now-row["last"] < cooldown or row["fingerprint"] == fingerprint):
            return None
        xp = (row["xp"] if row else 0) + amount
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?)", (guild, member, xp, now, fingerprint))
        return xp

    def jail(self, guild, member):
        return self.db.execute("SELECT * FROM jails WHERE guild=? AND member=? AND status='active'", (guild, member)).fetchone()

    def entity(self, guild, kind, entity, data):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO entity_snapshots VALUES (?,?,?,?)", (guild, kind, entity, json.dumps(data)))

    def entities(self, guild, kind):
        return [(row[0], json.loads(row[1])) for row in self.db.execute("SELECT entity,payload FROM entity_snapshots WHERE guild=? AND kind=?", (guild, kind))]

    def review(self, guild, member, reasons):
        existing = self.db.execute("SELECT status FROM reviews WHERE guild=? AND member=?", (guild, member)).fetchone()
        if existing and existing[0] in ("approved", "rejected"):
            return existing[0]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,'pending')", (guild, member, json.dumps(reasons)))
        return "pending"

    def flag(self, guild, member, source, detail):
        with self.db:
            # Explicit moderator clear decisions survive repeat observations;
            # ordinary enforcement results can be checked again on a new join.
            self.db.execute("INSERT INTO watch_flags VALUES (?,?,?,'pending',?,?) ON CONFLICT(guild,member,source) DO UPDATE SET status='pending',detail=excluded.detail,updated=excluded.updated WHERE watch_flags.status NOT IN ('pending','cleared')", (guild, member, source, detail, int(time.time())))

    def remap_role(self, guild, old, new):
        with self.db:
            self.db.execute("UPDATE member_roles SET role=? WHERE guild=? AND role=?", (new, guild, old))
            self.db.execute("UPDATE settings SET autorole=? WHERE guild=? AND autorole=?", (new, guild, old))
            for table in ("rewards", "jails", "selfroles", "recovered_role_members"):
                self.db.execute(f"UPDATE {table} SET role=? WHERE guild=? AND role=?", (new, guild, old))
            self.db.execute("UPDATE lock_states SET target=? WHERE guild=? AND kind='role' AND target=?",(new,guild,old))
            self.db.execute("UPDATE recovered_staff_roles SET role=? WHERE guild=? AND role=?",(new,guild,old))
            for key in ("verification", "jail"):
                config = self.core.option(guild, key)
                if config and config.get("role") == old:
                    config["role"] = new
                    self.db.execute("UPDATE options SET value=? WHERE guild=? AND key=?", (json.dumps(config), guild, key))
            for channel, snapshot in self.core.snapshots(guild):
                changed = False
                for overwrite in snapshot["overwrites"]:
                    if overwrite["role"] and overwrite["id"] == old:
                        overwrite["id"] = new
                        changed = True
                if changed:
                    self.db.execute("UPDATE channel_snapshots SET snapshot=? WHERE guild=? AND channel=?", (json.dumps(snapshot), guild, channel))
            self.db.execute("UPDATE entity_snapshots SET entity=? WHERE guild=? AND kind='role' AND entity=?", (new, guild, old))

    def remap_channel(self, guild, old, new):
        with self.db:
            self.db.execute("UPDATE lock_states SET channel=? WHERE guild=? AND channel=?", (new, guild, old))
            self.db.execute("UPDATE stickies SET channel=?,message=NULL WHERE guild=? AND channel=?", (new, guild, old))
            for key in ("protection",):
                config = self.core.option(guild, key)
                if config and config.get("channel") == old:
                    config["channel"] = new
                    self.db.execute("UPDATE options SET value=? WHERE guild=? AND key=?", (json.dumps(config), guild, key))
            for thread, snapshot in self.entities(guild, "thread"):
                if snapshot["parent"] == old:
                    snapshot["parent"] = new
                    self.db.execute("UPDATE entity_snapshots SET payload=? WHERE guild=? AND kind='thread' AND entity=?", (json.dumps(snapshot), guild, thread))
