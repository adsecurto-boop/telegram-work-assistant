import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import get_settings
from .database import create_db_engine, verify_schema_readiness


def compute_file_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _resolve_db_file(db_path: str) -> str:
    cleaned = db_path.replace("sqlite:///", "")
    return os.path.abspath(cleaned)


def backup_database(db_path: str, backup_dir: str) -> Dict[str, Any]:
    src_file = _resolve_db_file(db_path)
    if not os.path.exists(src_file):
        raise FileNotFoundError(f"Database file not found: {src_file}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"backup_{timestamp}.sqlite3")
    manifest_file = os.path.join(backup_dir, f"backup_{timestamp}_manifest.json")

    # Online SQLite backup API
    src_conn = sqlite3.connect(src_file)
    dst_conn = sqlite3.connect(backup_file)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Verify integrity of backup file
    chk_conn = sqlite3.connect(backup_file)
    try:
        cur = chk_conn.cursor()
        res = cur.execute("PRAGMA integrity_check;").fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {res}")
        try:
            rev_row = cur.execute("SELECT version_num FROM alembic_version;").fetchone()
            schema_revision = rev_row[0] if rev_row else "unknown"
        except sqlite3.OperationalError:
            schema_revision = "unmigrated"
    finally:
        chk_conn.close()

    checksum = compute_file_sha256(backup_file)
    manifest = {
        "backup_file": os.path.basename(backup_file),
        "schema_revision": schema_revision,
        "creation_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256_checksum": checksum,
        "source_app_version": "1.0.0",
        "source_db_file": os.path.basename(src_file),
    }

    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return {
        "backup_file": backup_file,
        "manifest_file": manifest_file,
        "manifest": manifest,
    }


def find_manifest_for_backup(backup_file: str) -> Optional[str]:
    base, _ = os.path.splitext(backup_file)
    candidate1 = f"{base}_manifest.json"
    if os.path.exists(candidate1):
        return candidate1
    candidate2 = os.path.join(os.path.dirname(backup_file), "manifest.json")
    if os.path.exists(candidate2):
        return candidate2
    return None


def verify_backup(backup_file: str, manifest_file: Optional[str] = None) -> Dict[str, Any]:
    if not os.path.exists(backup_file):
        raise FileNotFoundError(f"Backup file not found: {backup_file}")

    manifest_path = manifest_file or find_manifest_for_backup(backup_file)
    if not manifest_path or not os.path.exists(manifest_path):
        raise ValueError(f"Backup manifest file not found for: {backup_file}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt manifest JSON: {exc}") from exc

    expected_checksum = manifest.get("sha256_checksum")
    if not expected_checksum:
        raise ValueError("Manifest missing required 'sha256_checksum' field.")

    actual_checksum = compute_file_sha256(backup_file)
    if actual_checksum != expected_checksum:
        raise ValueError(
            f"Checksum verification failed! Expected: {expected_checksum}, Actual: {actual_checksum}"
        )

    # Verify SQLite integrity check
    conn = None
    try:
        conn = sqlite3.connect(backup_file)
        cur = conn.cursor()
        res = cur.execute("PRAGMA integrity_check;").fetchone()
        if not res or res[0] != "ok":
            raise ValueError(f"SQLite PRAGMA integrity_check failed: {res}")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Corrupt or invalid SQLite database: {exc}") from exc
    finally:
        if conn:
            conn.close()

    # Verify schema readiness
    engine = create_db_engine(f"sqlite:///{backup_file}")
    try:
        verify_schema_readiness(engine)
    except Exception as exc:
        raise ValueError(f"Schema readiness check failed: {exc}") from exc
    finally:
        engine.dispose()

    return {
        "valid": True,
        "manifest": manifest,
        "integrity": "ok",
    }


def restore_database(
    backup_file: str,
    target_db_path: str,
    confirm_restore: bool = False,
    manifest_file: Optional[str] = None,
) -> Dict[str, Any]:
    if not confirm_restore:
        raise ValueError(
            "Restore operation refused: --confirm-restore flag is mandatory to prevent accidental overwrite."
        )

    # Pre-flight verification of backup file
    verify_backup(backup_file, manifest_file)

    target_file = _resolve_db_file(target_db_path)
    target_dir = os.path.dirname(target_file) or "."
    os.makedirs(target_dir, exist_ok=True)

    pre_restore_backup: Optional[str] = None
    if os.path.exists(target_file):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pre_restore_backup = f"{target_file}.pre_restore_{timestamp}.bak"
        shutil.copy2(target_file, pre_restore_backup)

    # Perform atomic replacement
    temp_target = f"{target_file}.tmp_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    try:
        shutil.copy2(backup_file, temp_target)
        # Atomic file replacement
        os.replace(temp_target, target_file)
        # Clean up stale WAL / SHM files if present
        for aux in (f"{target_file}-wal", f"{target_file}-shm"):
            if os.path.exists(aux):
                try:
                    os.remove(aux)
                except OSError:
                    pass
    except Exception as exc:
        recovery_msg = (
            f"Atomic replacement failed: {exc}. "
            + (f"Original database preserved at '{pre_restore_backup}'." if pre_restore_backup else "")
        )
        raise RuntimeError(recovery_msg) from exc

    # Post-restore verification
    engine = create_db_engine(f"sqlite:///{target_file}")
    try:
        conn = sqlite3.connect(target_file)
        res = conn.cursor().execute("PRAGMA integrity_check;").fetchone()
        conn.close()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Restored database failed integrity check: {res}")
        verify_schema_readiness(engine)
    finally:
        engine.dispose()

    return {
        "status": "restored",
        "target_db": target_file,
        "pre_restore_backup": pre_restore_backup,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Support Copilot Operations: Backup and Restore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # backup
    p_backup = subparsers.add_parser("backup", help="Create an online SQLite backup with manifest")
    p_backup.add_argument("--db", help="Path to SQLite database (default: from configuration)")
    p_backup.add_argument("--out", help="Directory to store backups (default: <data_dir>/backups)")

    # verify-backup
    p_verify = subparsers.add_parser("verify-backup", help="Verify backup checksum and integrity")
    p_verify.add_argument("backup_file", help="Path to backup .sqlite3 file")
    p_verify.add_argument("--manifest", help="Path to manifest file (optional)")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore database from a verified backup")
    p_restore.add_argument("backup_file", help="Path to backup .sqlite3 file")
    p_restore.add_argument("--db", help="Target database path (default: from configuration)")
    p_restore.add_argument("--manifest", help="Path to manifest file (optional)")
    p_restore.add_argument("--confirm-restore", action="store_true", help="Explicit confirmation flag required to restore")

    args = parser.parse_args()
    settings = get_settings()

    try:
        if args.command == "backup":
            db_path = args.db or settings.db_path
            backup_dir = args.out or os.path.join(settings.data_dir, "backups")
            result = backup_database(db_path, backup_dir)
            print(f"Backup created successfully:")
            print(f"  Backup File:   {result['backup_file']}")
            print(f"  Manifest File: {result['manifest_file']}")
            print(f"  Checksum:      {result['manifest']['sha256_checksum']}")
            print(f"  Revision:      {result['manifest']['schema_revision']}")

        elif args.command == "verify-backup":
            result = verify_backup(args.backup_file, args.manifest)
            print(f"Backup verification PASSED:")
            print(f"  File:      {args.backup_file}")
            print(f"  Checksum:  {result['manifest']['sha256_checksum']}")
            print(f"  Revision:  {result['manifest']['schema_revision']}")
            print(f"  Integrity: {result['integrity']}")

        elif args.command == "restore":
            target_db = args.db or settings.db_path
            result = restore_database(
                backup_file=args.backup_file,
                target_db_path=target_db,
                confirm_restore=args.confirm_restore,
                manifest_file=args.manifest,
            )
            print(f"Database restored successfully:")
            print(f"  Target DB:          {result['target_db']}")
            print(f"  Pre-restore Backup: {result['pre_restore_backup'] or 'None (new database)'}")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
