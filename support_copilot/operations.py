import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import get_settings
from .database import (
    CURRENT_SCHEMA_REVISION,
    REQUIRED_TABLES_CURRENT,
    create_db_engine,
    verify_schema_readiness,
)


MANIFEST_FORMAT_VERSION = 1
APPLICATION_VERSION = "1.0.0"


def compute_file_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _resolve_db_file(db_path: str) -> str:
    if "://" in db_path and not db_path.startswith("sqlite:///"):
        raise ValueError("Only a SQLite file path or sqlite:/// URL is supported.")
    cleaned = db_path[len("sqlite:///"):] if db_path.startswith("sqlite:///") else db_path
    if not cleaned.strip():
        raise ValueError("Database path cannot be empty.")
    return os.path.abspath(cleaned)


def _unique_stamp() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"


def _read_schema_revision(db_file: str) -> str:
    conn = sqlite3.connect(f"file:{Path(db_file).as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version;").fetchone()
        return str(row[0]) if row else "unknown"
    except sqlite3.OperationalError:
        return "unmigrated"
    finally:
        conn.close()


def _verify_sqlite_file(db_file: str, require_current_schema: bool = True) -> str:
    try:
        conn = sqlite3.connect(f"file:{Path(db_file).as_posix()}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Corrupt or invalid SQLite database: {exc}") from exc
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite PRAGMA integrity_check failed: {result}")

    revision = _read_schema_revision(db_file)
    if require_current_schema and revision != CURRENT_SCHEMA_REVISION:
        raise ValueError(
            f"Incompatible schema revision '{revision}'; expected '{CURRENT_SCHEMA_REVISION}'."
        )
    if require_current_schema:
        engine = create_db_engine(f"sqlite:///{db_file}")
        try:
            verify_schema_readiness(engine, required_tables=REQUIRED_TABLES_CURRENT)
        except Exception as exc:
            raise ValueError(f"Schema readiness check failed: {exc}") from exc
        finally:
            engine.dispose()
    return revision


def backup_database(db_path: str, backup_dir: str) -> Dict[str, Any]:
    src_file = _resolve_db_file(db_path)
    if not os.path.exists(src_file):
        raise FileNotFoundError(f"Database file not found: {src_file}")

    os.makedirs(backup_dir, exist_ok=True)
    stamp = _unique_stamp()
    backup_file = os.path.join(backup_dir, f"backup_{stamp}.sqlite3")
    manifest_file = os.path.join(backup_dir, f"backup_{stamp}_manifest.json")

    # Online SQLite backup API
    try:
        src_conn = sqlite3.connect(src_file)
        dst_conn = sqlite3.connect(backup_file)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        schema_revision = _verify_sqlite_file(backup_file, require_current_schema=False)
    except Exception:
        if os.path.exists(backup_file):
            os.remove(backup_file)
        raise

    checksum = compute_file_sha256(backup_file)
    manifest = {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "backup_file": os.path.basename(backup_file),
        "schema_revision": schema_revision,
        "creation_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256_checksum": checksum,
        "source_app_version": APPLICATION_VERSION,
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

    if not isinstance(manifest, dict):
        raise ValueError("Backup manifest must contain a JSON object.")
    required_fields = {
        "manifest_format_version",
        "backup_file",
        "schema_revision",
        "creation_timestamp",
        "sha256_checksum",
        "source_app_version",
    }
    missing_fields = sorted(required_fields - set(manifest))
    if missing_fields:
        raise ValueError(f"Manifest missing required fields: {', '.join(missing_fields)}")
    if manifest["manifest_format_version"] != MANIFEST_FORMAT_VERSION:
        raise ValueError("Unsupported backup manifest format version.")
    if manifest["backup_file"] != os.path.basename(backup_file):
        raise ValueError("Manifest backup filename does not match the selected backup file.")

    expected_checksum = manifest["sha256_checksum"]

    actual_checksum = compute_file_sha256(backup_file)
    if actual_checksum != expected_checksum:
        raise ValueError(
            f"Checksum verification failed! Expected: {expected_checksum}, Actual: {actual_checksum}"
        )

    actual_revision = _verify_sqlite_file(backup_file, require_current_schema=True)
    if manifest["schema_revision"] != actual_revision:
        raise ValueError("Manifest schema revision does not match the backup database.")

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

    # Stage and verify a separate copy before touching the active database.
    temp_target = f"{target_file}.restore_stage_{_unique_stamp()}"
    shutil.copy2(backup_file, temp_target)
    try:
        _verify_sqlite_file(temp_target, require_current_schema=True)
    except Exception:
        if os.path.exists(temp_target):
            os.remove(temp_target)
        raise

    pre_restore_backup: Optional[str] = None
    pre_restore_manifest: Optional[str] = None
    try:
        if os.path.exists(target_file):
            # Checkpoint committed WAL data before taking the online recovery copy.
            checkpoint_conn = sqlite3.connect(target_file, timeout=1.0)
            try:
                checkpoint = checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
                if checkpoint and checkpoint[0] != 0:
                    raise RuntimeError(
                        "Restore refused because the active database is busy. Stop the API and desktop application, then retry."
                    )
            finally:
                checkpoint_conn.close()
            recovery = backup_database(target_file, os.path.join(target_dir, "backups", "pre_restore"))
            pre_restore_backup = recovery["backup_file"]
            pre_restore_manifest = recovery["manifest_file"]
    except Exception:
        if os.path.exists(temp_target):
            os.remove(temp_target)
        raise

    replaced = False
    try:
        os.replace(temp_target, target_file)
        replaced = True
        # Clean up stale WAL / SHM files if present
        for aux in (f"{target_file}-wal", f"{target_file}-shm"):
            if os.path.exists(aux):
                try:
                    os.remove(aux)
                except OSError:
                    pass
        _verify_sqlite_file(target_file, require_current_schema=True)
    except Exception as exc:
        if os.path.exists(temp_target):
            os.remove(temp_target)
        rollback_note = ""
        if replaced and pre_restore_backup and os.path.exists(pre_restore_backup):
            rollback_stage = f"{target_file}.rollback_{_unique_stamp()}"
            shutil.copy2(pre_restore_backup, rollback_stage)
            os.replace(rollback_stage, target_file)
            rollback_note = " The verified pre-restore backup was automatically restored."
        elif replaced and not pre_restore_backup:
            quarantine = f"{target_file}.failed_restore_{_unique_stamp()}"
            os.replace(target_file, quarantine)
            rollback_note = f" The failed new database was quarantined as '{quarantine}'."
        recovery_msg = (
            f"Restore failed: {exc}. "
            + (f"Recovery backup is preserved at '{pre_restore_backup}'." if pre_restore_backup else "")
            + rollback_note
        )
        raise RuntimeError(recovery_msg) from exc

    return {
        "status": "restored",
        "target_db": target_file,
        "pre_restore_backup": pre_restore_backup,
        "pre_restore_manifest": pre_restore_manifest,
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
