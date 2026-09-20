import os
import sys
import argparse
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional
from alembic.config import Config
from alembic import command
from .config import Settings, get_settings
from .database import create_db_engine, verify_schema_readiness
from .logger import logger

class MigrationError(Exception):
    """Raised when a database migration fails."""
    pass

def check_sqlite_integrity(db_file_path: str) -> bool:
    """Runs PRAGMA quick_check and PRAGMA integrity_check on a SQLite database file."""
    if not os.path.exists(db_file_path) or os.path.getsize(db_file_path) == 0:
        return True
    conn = sqlite3.connect(db_file_path)
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA quick_check;")
        res_quick = cursor.fetchall()
        if not res_quick or res_quick[0][0] != "ok":
            return False
        cursor.execute("PRAGMA integrity_check;")
        res_integrity = cursor.fetchall()
        if not res_integrity or res_integrity[0][0] != "ok":
            return False
        return True
    except Exception as exc:
        logger.error(f"Integrity check failed with error: {exc}")
        return False
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        conn.close()

def create_online_backup(source_path: str, backup_path: str) -> None:
    """Uses SQLite online backup API to copy database including committed WAL data safely."""
    source_conn = sqlite3.connect(source_path)
    backup_conn = sqlite3.connect(backup_path)
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()

def restore_backup_safely(backup_path: str, target_path: str) -> None:
    """
    Restores through a verified temporary destination followed by atomic replacement
    after handles are closed.
    """
    temp_restore = f"{target_path}.temp_restore_{uuid.uuid4().hex[:8]}"
    create_online_backup(backup_path, temp_restore)
    if not check_sqlite_integrity(temp_restore):
        if os.path.exists(temp_restore):
            os.remove(temp_restore)
        raise MigrationError(f"Integrity check failed on restored temporary database: {temp_restore}")

    import gc
    gc.collect()

    # Atomic replacement
    try:
        os.replace(temp_restore, target_path)
    except PermissionError:
        # Fallback on Windows if an OS file handle lock is lingering
        import time
        gc.collect()
        time.sleep(0.05)
        try:
            os.replace(temp_restore, target_path)
        except PermissionError:
            # Fallback to online backup copy if os.replace is locked by Windows
            create_online_backup(temp_restore, target_path)
            if os.path.exists(temp_restore):
                os.remove(temp_restore)

    if not check_sqlite_integrity(target_path):
        raise MigrationError(f"Integrity check failed on database after atomic replacement: {target_path}")

class MigrationRunner:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        alembic_ini_path: str = "support_copilot/alembic.ini",
    ):
        self.settings = settings or get_settings()
        self.alembic_ini_path = alembic_ini_path

    def resolve_sqlite_path(self) -> str:
        return self.settings.get_canonical_db_path()

    def run_upgrade(
        self, target_revision: str = "head", _fail_after_backup: bool = False
    ) -> str:
        """
        Executes Alembic upgrade with pre-migration online backup and automatic rollback on failure.
        Returns the path to the backup created, or empty string if database was newly created.
        """
        db_file = self.resolve_sqlite_path()
        backup_file: Optional[str] = None
        was_existing_db = os.path.exists(db_file) and os.path.getsize(db_file) > 0

        # Ensure directory exists
        db_dir = os.path.dirname(db_file)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        if was_existing_db:
            if not check_sqlite_integrity(db_file):
                raise MigrationError("Pre-migration integrity check failed on source database.")

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            unique_id = uuid.uuid4().hex[:8]
            base_dir = os.path.dirname(db_file) or "."
            file_name = os.path.basename(db_file)
            backup_file = os.path.join(base_dir, f"{file_name}.backup_{timestamp}_{unique_id}.db")

            create_online_backup(db_file, backup_file)
            logger.info(f"Created verified online backup at {backup_file}")

            if not check_sqlite_integrity(backup_file):
                raise MigrationError("Post-backup integrity check failed on backup file.")

        alembic_cfg = Config(self.alembic_ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

        try:
            if _fail_after_backup:
                raise RuntimeError("Simulated failure inside migration runner after backup creation.")
            command.upgrade(alembic_cfg, target_revision)

            engine = create_db_engine(f"sqlite:///{db_file}")
            try:
                verify_schema_readiness(engine)
            finally:
                engine.dispose()

        except Exception as exc:
            logger.error(
                f"Migration failed: {exc}. Initiating rollback.",
                extra={"failure_category": "migration_failure"},
            )
            if was_existing_db and backup_file and os.path.exists(backup_file):
                restore_backup_safely(backup_file, db_file)
                logger.info(f"Successfully restored database from backup {backup_file}")
            elif not was_existing_db:
                # If migration on a newly created database failed, remove/quarantine partial database
                if os.path.exists(db_file):
                    quarantine_file = f"{db_file}.failed_quarantine_{uuid.uuid4().hex[:8]}"
                    os.replace(db_file, quarantine_file)
                    logger.warning(f"Quarantined partial new database to {quarantine_file}")
            raise MigrationError(f"Migration to '{target_revision}' failed: {exc}") from exc

        return backup_file or ""

    def run_downgrade(
        self, target_revision: str = "base", allow_downgrade: bool = False
    ) -> str:
        """
        Downgrade is destructive and must:
        - create a verified backup,
        - require explicit confirmation (allow_downgrade=True),
        - preserve the backup.
        """
        if not allow_downgrade:
            raise MigrationError(
                "Downgrade is a destructive operation. You must provide --allow-downgrade to proceed."
            )

        db_file = self.resolve_sqlite_path()
        backup_file: Optional[str] = None

        if os.path.exists(db_file) and os.path.getsize(db_file) > 0:
            if not check_sqlite_integrity(db_file):
                raise MigrationError("Pre-downgrade integrity check failed on source database.")

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            unique_id = uuid.uuid4().hex[:8]
            base_dir = os.path.dirname(db_file) or "."
            file_name = os.path.basename(db_file)
            backup_file = os.path.join(base_dir, f"{file_name}.backup_{timestamp}_{unique_id}.db")

            create_online_backup(db_file, backup_file)
            logger.info(f"Created pre-downgrade online backup at {backup_file}")

            if not check_sqlite_integrity(backup_file):
                raise MigrationError("Post-backup integrity check failed on backup file.")

        alembic_cfg = Config(self.alembic_ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

        try:
            command.downgrade(alembic_cfg, target_revision)
        except Exception as exc:
            if backup_file and os.path.exists(backup_file):
                restore_backup_safely(backup_file, db_file)
            raise MigrationError(f"Downgrade to '{target_revision}' failed: {exc}") from exc

        return backup_file or ""

    def inspect_current(self) -> None:
        """Read-only inspection of current revision."""
        db_file = self.resolve_sqlite_path()
        alembic_cfg = Config(self.alembic_ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
        command.current(alembic_cfg)

def main():
    parser = argparse.ArgumentParser(description="Support Copilot Safe Migration Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upgrade_parser = subparsers.add_parser("upgrade", help="Run database upgrade")
    upgrade_parser.add_argument("revision", nargs="?", default="head", help="Target revision (default: head)")

    current_parser = subparsers.add_parser("current", help="Inspect current database revision")

    downgrade_parser = subparsers.add_parser("downgrade", help="Run database downgrade")
    downgrade_parser.add_argument("revision", nargs="?", default="base", help="Target revision (default: base)")
    downgrade_parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        default=False,
        help="Explicitly allow destructive downgrade",
    )

    args = parser.parse_args()
    runner = MigrationRunner()

    try:
        if args.command == "upgrade":
            backup = runner.run_upgrade(args.revision)
            print(f"Upgrade to '{args.revision}' completed successfully.")
            if backup:
                print(f"Pre-migration backup preserved at: {backup}")
        elif args.command == "current":
            runner.inspect_current()
        elif args.command == "downgrade":
            backup = runner.run_downgrade(args.revision, allow_downgrade=args.allow_downgrade)
            print(f"Downgrade to '{args.revision}' completed successfully.")
            if backup:
                print(f"Pre-downgrade backup preserved at: {backup}")
    except MigrationError as err:
        print(f"Migration Error: {err}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
