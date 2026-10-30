# Kuro Recovery Runbook

This guide is for the person who maintains Kuro's hosting. Use a recovery bundle to move saved state to a replacement deployment after a failure. This does not create a Discord application, copy a bot token, or start a second running bot automatically.

## Create and keep a backup

The application owner can use `/backups create` and `/backups status` in Discord. The verified ZIP stays under `backups` on the current data volume. `/backups setup enabled:true interval_hours:24 keep:1` enables a schedule; the default is off.

Download the chosen archive privately using Railway's volume file tools. Keep a copy outside Railway's current volume. A complete bundle includes state and saved content from all of Kuro's servers, so keep it private. Nothing is posted into Discord by the backup command.

You can also create one locally while Kuro is running. The SQLite backup API reads a consistent committed database snapshot, including committed WAL transactions:

```powershell
.\.venv\Scripts\python.exe scripts\recovery_bundle.py create --data-dir 'D:\kuro bot\data' --output 'D:\Kuro Backups\kuro.zip'
```

The output filename must not already exist. Only the SQLite database and referenced attachments with Kuro's generated filenames are included. Credential files, unrelated files, and files that escape the data directory are excluded. Missing saved attachment files are reported in the manifest rather than claimed as backed up.

Limits are 512 MiB of saved data and 10,000 files per archive. Automatic retention removes only previously recorded, bot-generated archives. Keep enough free space for the live data, retained ZIPs, and a new ZIP before the oldest one is removed. A failed backup does not erase the previous verified archive.

## Verify before restoring

Use a trusted archive. Checksums detect changed or damaged files; they do not establish who created an archive if someone replaces both its files and manifest.

```powershell
.\.venv\Scripts\python.exe scripts\recovery_bundle.py verify 'D:\Kuro Backups\kuro.zip'
```

Verification checks file names, declared sizes, SHA256 checksums, and SQLite integrity. It rejects unsafe paths, duplicate entries, symlinks, and oversized expanded contents. Attachment links may already have expired even when the database itself is intact.

## Restore without overwriting live data

Choose a destination that does not exist:

```powershell
.\.venv\Scripts\python.exe scripts\recovery_bundle.py restore 'D:\Kuro Backups\kuro.zip' --destination 'D:\Kuro Standby\data'
.\.venv\Scripts\python.exe scripts\recovery_bundle.py inspect --data-dir 'D:\Kuro Standby\data'
```

The live directory is left alone. The restored database is marked paused, and Kuro refuses to start from it until explicitly resumed. An interrupted or failed restore keeps an `RESTORE_INCOMPLETE` marker and cannot be resumed; start again with a verified archive and another new destination.

The inspect command shows pending bans, active jails, queued structure copies, and membership rules without printing saved message bodies or credentials. Also review relevant settings in the restored SQLite database. Backups represent their creation time: actions may have run or settings may have changed since then. Overdue pending bans can execute as soon as the restored bot starts.

## Activate one replacement

1. Stop or retire the old running process. Do not run two active copies against the same Discord application.
2. Review queued actions and server settings in the restored database. Adjust stale actions before activation if necessary.
3. Explicitly resume the restored directory:

```powershell
.\.venv\Scripts\python.exe scripts\recovery_bundle.py resume --data-dir 'D:\Kuro Standby\data' --acknowledge-pending-actions
```

4. Configure the replacement process to use that directory through `KURO_DATA_DIR`, or place the restored data on its mounted volume. Supply the existing bot token through the hosting environment; it is not in the archive.
5. Start exactly one bot. Check its connection, `/features_status`, and `/recovery_status`. Test with a disposable channel before relying on restored recovery settings.

For a local start:

```powershell
$env:KURO_DATA_DIR = 'D:\Kuro Standby\data'
.\.venv\Scripts\python.exe bot.py
```

New hosting services, automatic failover, bot self-reinstallation, and Discord application creation are not performed by these tools. They require a separate deployment decision and the required account access.
