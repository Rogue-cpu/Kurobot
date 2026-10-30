"""Portable recovery bundles. Never includes .env, tokens, or arbitrary files."""
import hashlib
import json
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 512 * 1024**2
FILE_NAME = re.compile(r"attachments/[0-9]+-[0-9]+\Z")


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def create_bundle(data_dir, output):
    data_dir, output = Path(data_dir).resolve(), Path(output).resolve()
    database = data_dir / "kuro.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("Choose a data directory containing a regular kuro.sqlite3 file.")
    if output.exists():
        raise ValueError("The output already exists. Choose a new filename.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kuro-snapshot-") as temporary:
        snapshot = Path(temporary) / "kuro.sqlite3"
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        wanted = set()
        try:
            source.backup(target)
            if snapshot.stat().st_size>MAX_BYTES:
                raise ValueError("Database exceeds the 512 MiB recovery bundle limit.")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The database failed its integrity check.")
            for row in target.execute("SELECT payload FROM message_backups"):
                for attachment in json.loads(row[0]).get("attachments", []):
                    name = attachment.get("file", "")
                    if re.fullmatch(r"[0-9]+-[0-9]+", name):
                        wanted.add("attachments/" + name)
                        if len(wanted)>=10000:
                            raise ValueError("Bundle exceeds its 10,000-file limit.")
        finally:
            target.close()
            source.close()
        manifest = {"version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "files": {}, "missing_attachments": []}
        used = 0
        handle = output.open("xb")
        try:
            with handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for name in ["kuro.sqlite3", *sorted(wanted)]:
                    path = snapshot if name == "kuro.sqlite3" else data_dir / name
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(snapshot.parent if name == "kuro.sqlite3" else data_dir):
                        if name == "kuro.sqlite3":
                            raise ValueError("Database snapshot is unavailable.")
                        manifest["missing_attachments"].append(name)
                        continue
                    # Stage attachments so a concurrent retention cleanup cannot
                    # produce a checksum for different bytes than the ZIP entry.
                    staged = snapshot if name == "kuro.sqlite3" else Path(temporary) / Path(name).name
                    if name != "kuro.sqlite3":
                        try:
                            shutil.copyfile(path, staged)
                        except FileNotFoundError:
                            manifest["missing_attachments"].append(name)
                            continue
                    size = staged.stat().st_size
                    used += size
                    if used > MAX_BYTES or len(manifest["files"]) >= 10000:
                        raise ValueError("Bundle exceeds the 512 MiB or 10,000-file recovery limit.")
                    manifest["files"][name] = {"size": size, "sha256": checksum(staged)}
                    archive.write(staged, name)
                    if name != "kuro.sqlite3":
                        staged.unlink()
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        except Exception:
            output.unlink(missing_ok=True)
            raise
    try:
        verify_bundle(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return manifest


def verify_bundle(bundle):
    with zipfile.ZipFile(bundle) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(names) > 10001:
            raise ValueError("Duplicate or excessive archive entries.")
        if "manifest.json" not in names or "kuro.sqlite3" not in names:
            raise ValueError("This is not a Kuro recovery bundle.")
        if archive.getinfo("manifest.json").file_size > 2 * 1024**2:
            raise ValueError("The bundle manifest is too large.")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported recovery bundle format.")
        if set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("Archive contents do not match its manifest.")
        if sum(entry.file_size for entry in entries) > MAX_BYTES + 2 * 1024**2:
            raise ValueError("Expanded archive exceeds the recovery limit.")
        with tempfile.TemporaryDirectory(prefix="kuro-verify-") as temporary:
            db_path = Path(temporary) / "kuro.sqlite3"
            for entry in entries:
                if entry.filename != "manifest.json" and entry.filename != "kuro.sqlite3" and not FILE_NAME.fullmatch(entry.filename):
                    raise ValueError("Unexpected file or unsafe path in bundle.")
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
                    raise ValueError("Links and directory entries are not allowed.")
                if entry.filename == "manifest.json":
                    continue
                expected = manifest["files"][entry.filename]
                if expected.get("size") != entry.file_size:
                    raise ValueError("A saved file has an incorrect size.")
                digest = hashlib.sha256()
                destination = db_path.open("wb") if entry.filename == "kuro.sqlite3" else None
                try:
                    with archive.open(entry) as source:
                        for block in iter(lambda: source.read(1024**2), b""):
                            digest.update(block)
                            if destination:
                                destination.write(block)
                finally:
                    if destination:
                        destination.close()
                if digest.hexdigest() != expected.get("sha256"):
                    raise ValueError("A saved file failed its checksum.")
            db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("The archived database is not intact.")
            finally:
                db.close()
    return manifest


def restore_bundle(bundle, destination):
    """Restore to a new directory, paused until the operator explicitly resumes."""
    manifest = verify_bundle(bundle)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    marker=destination / "RESTORE_INCOMPLETE"
    marker.write_text("This restore is incomplete. Do not start Kuro here.",encoding="utf-8")
    try:
        with zipfile.ZipFile(bundle) as archive:
            for name in manifest["files"]:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if checksum(target)!=manifest["files"][name]["sha256"]:
                    raise ValueError("Bundle changed during restore; the partial directory remains paused.")
        db = sqlite3.connect(destination / "kuro.sqlite3")
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO options VALUES (0,'restore_paused','true')")
        finally:
            db.close()
        marker.unlink()
    except Exception:
        # Keep a failed restore available for inspection; never recurse-delete an
        # operator-supplied destination or touch the existing live directory.
        raise
    return manifest


def resume_restored(data_dir):
    if (Path(data_dir).resolve()/"RESTORE_INCOMPLETE").exists():
        raise ValueError("This restore is incomplete. Restore a verified bundle into another new directory.")
    database = Path(data_dir).resolve() / "kuro.sqlite3"
    if not database.is_file():
        raise ValueError("No database exists in that directory.")
    db = sqlite3.connect(database)
    try:
        with db:
            db.execute("DELETE FROM options WHERE guild=0 AND key='restore_paused'")
    finally:
        db.close()


def inspect_data(data_dir):
    database=Path(data_dir).resolve()/"kuro.sqlite3"
    db=sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)
    db.row_factory=sqlite3.Row
    try:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result={"restore_paused":bool(db.execute("SELECT 1 FROM options WHERE guild=0 AND key='restore_paused'").fetchone()),"pending_actions":{}}
        queries={
            "scheduled_bans":"SELECT id,guild,member,due FROM scheduled_bans WHERE status='pending'",
            "jails":"SELECT guild,member,due FROM jails WHERE status='active'",
            "blueprint_jobs":"SELECT id,guild,status FROM blueprint_jobs WHERE status IN ('queued','working')",
            "watches":"SELECT guild,source,action FROM watches",
        }
        for table,query in queries.items():
            if table in tables:
                result["pending_actions"][table]=[dict(row) for row in db.execute(query)]
        return result
    finally:
        db.close()
