import asyncio
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BACKUPS = 3
BACKUP_INTERVAL = 86400  # 24 hours


def backup_dir(db_path: str) -> Path:
    return Path(db_path).parent / "backups"


def list_backups(db_path: str) -> list[dict]:
    bdir = backup_dir(db_path)
    if not bdir.exists():
        return []
    backups = []
    for f in sorted(bdir.glob("explorer-*.db"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "path": str(f),
            "size_kb": round(stat.st_size / 1024, 1),
            "created_at": int(stat.st_mtime),
        })
    return backups


def create_backup(db_path: str) -> str | None:
    src = Path(db_path)
    if not src.exists():
        log.warning("Cannot backup: database file %s not found", db_path)
        return None

    bdir = backup_dir(db_path)
    bdir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"explorer-{timestamp}.db"

    # SQLite's online backup API instead of a filesystem copy: it takes a
    # transactionally-consistent snapshot of the *live* WAL-mode database (a raw
    # `cp` can capture a torn write, and the separate -wal it left behind was a
    # fragile way to paper over that). The result is a single self-contained file
    # with no -wal sidecar to keep in sync.
    src_conn = sqlite3.connect(db_path)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            with dest_conn:
                src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    _prune_old_backups(db_path)
    log.info("Database backup created: %s (%.1f KB)", dest.name, dest.stat().st_size / 1024)
    return str(dest)


def restore_backup(db_path: str, backup_filename: str) -> bool:
    bdir = backup_dir(db_path)
    # The filename arrives from a URL path segment. Containment check: it must be
    # a bare filename resolving to a file directly inside the backups dir —
    # rejects separator/`..` tricks (incl. Windows backslashes, where a single
    # segment like `..\..\x` would otherwise traverse).
    src = (bdir / backup_filename).resolve()
    if Path(backup_filename).name != backup_filename or src.parent != bdir.resolve():
        log.error("Rejected restore filename outside backups dir: %r", backup_filename)
        return False
    if not src.exists():
        log.error("Backup file not found: %s", src)
        return False

    dest = Path(db_path)
    pre_restore = bdir / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
    if dest.exists():
        shutil.copy2(str(dest), str(pre_restore))
        log.info("Pre-restore snapshot saved: %s", pre_restore.name)

    shutil.copy2(str(src), str(dest))

    for suffix in ("-wal", "-shm"):
        leftover = Path(f"{db_path}{suffix}")
        if leftover.exists():
            leftover.unlink()

    backup_wal = Path(f"{src}-wal")
    if backup_wal.exists():
        shutil.copy2(str(backup_wal), f"{db_path}-wal")

    log.info("Database restored from %s", backup_filename)
    return True


def _prune_old_backups(db_path: str) -> None:
    bdir = backup_dir(db_path)
    backups = sorted(bdir.glob("explorer-*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)
        wal = Path(f"{old}-wal")
        wal.unlink(missing_ok=True)
        log.info("Pruned old backup: %s", old.name)


def needs_backup(db_path: str) -> bool:
    bdir = backup_dir(db_path)
    if not bdir.exists():
        return True
    latest = sorted(bdir.glob("explorer-*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not latest:
        return True
    return (time.time() - latest[0].stat().st_mtime) >= BACKUP_INTERVAL


async def run_backup_loop(db_path: str, engine=None) -> None:
    log.info("Backup loop started (interval: %ds)", BACKUP_INTERVAL)
    if needs_backup(db_path):
        log.info("No recent backup found — creating one now")
        try:
            create_backup(db_path)
        except Exception:
            log.exception("Startup backup failed")
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            create_backup(db_path)
            log.info("Next backup in %d hours", BACKUP_INTERVAL // 3600)
        except Exception:
            log.exception("Backup failed")
        if engine:
            try:
                await engine.refresh_repeaters()
            except Exception:
                log.exception("Repeater refresh during backup failed")
