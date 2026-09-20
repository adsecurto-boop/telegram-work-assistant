import json
import os
import sqlite3
import pytest

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.migration_runner import MigrationRunner
from support_copilot.models import Base, CapturedEvent
from support_copilot.operations import backup_database, verify_backup, restore_database


@pytest.fixture
def populated_db(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "ops_test.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{db_file}")

    # Run migrations to head
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    # Insert sample record
    engine = create_db_engine(f"sqlite:///{db_file}")
    session_factory = create_session_factory(engine)
    db = session_factory()
    from datetime import datetime, timezone
    evt = CapturedEvent(
        provider="manual",
        event_id="evt-ops-1",
        event_type="message.received",
        occurred_at=datetime.now(timezone.utc),
        actor_id="tester",
        actor_role="client",
        conversation_id="conv-ops-1",
        payload_text="Data that must survive backup and restore",
        schema_version=1,
        correlation_id="corr-ops-1",
    )
    db.add(evt)
    db.commit()
    db.close()
    engine.dispose()

    return {"db_file": str(db_file), "data_dir": str(data_dir), "backup_dir": str(tmp_path / "backups")}


def test_online_backup_and_manifest_generation(populated_db):
    result = backup_database(populated_db["db_file"], populated_db["backup_dir"])
    assert os.path.exists(result["backup_file"])
    assert os.path.exists(result["manifest_file"])

    manifest = result["manifest"]
    assert manifest["sha256_checksum"]
    assert manifest["schema_revision"] == "005_phase7"
    assert manifest["source_app_version"] == "1.0.0"

    # Verify backup with helper
    verification = verify_backup(result["backup_file"], result["manifest_file"])
    assert verification["valid"] is True
    assert verification["integrity"] == "ok"


def test_verify_backup_detects_checksum_mismatch(populated_db):
    result = backup_database(populated_db["db_file"], populated_db["backup_dir"])

    # Tamper with the backup file
    with open(result["backup_file"], "ab") as f:
        f.write(b"tampered-bytes")

    with pytest.raises(ValueError) as exc:
        verify_backup(result["backup_file"], result["manifest_file"])
    assert "checksum verification failed" in str(exc.value).lower()


def test_verify_backup_detects_missing_manifest(populated_db, tmp_path):
    isolated_backup = tmp_path / "orphan.sqlite3"
    isolated_backup.write_bytes(b"some-bytes")

    with pytest.raises(ValueError) as exc:
        verify_backup(str(isolated_backup))
    assert "manifest file not found" in str(exc.value).lower()


def test_verify_backup_detects_corrupt_database(populated_db, tmp_path):
    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_db.write_bytes(b"not a real sqlite database header")
    manifest_file = tmp_path / "corrupt_manifest.json"

    import hashlib
    h = hashlib.sha256(b"not a real sqlite database header").hexdigest()
    manifest_file.write_text(
        json.dumps({"backup_file": "corrupt.sqlite3", "sha256_checksum": h, "schema_revision": "005_phase7"})
    )

    with pytest.raises(ValueError) as exc:
        verify_backup(str(corrupt_db), str(manifest_file))
    assert "corrupt or invalid sqlite" in str(exc.value).lower()


def test_verify_backup_detects_incompatible_schema(tmp_path):
    # Create a valid SQLite db that has an empty/incompatible schema
    incomp_db = tmp_path / "incompatible.sqlite3"
    conn = sqlite3.connect(str(incomp_db))
    conn.execute("CREATE TABLE some_random_table (id INT);")
    conn.commit()
    conn.close()

    import hashlib
    h = hashlib.sha256(incomp_db.read_bytes()).hexdigest()
    manifest_file = tmp_path / "incompatible_manifest.json"
    manifest_file.write_text(
        json.dumps({
            "backup_file": "incompatible.sqlite3",
            "sha256_checksum": h,
            "schema_revision": "unknown",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        verify_backup(str(incomp_db), str(manifest_file))
    assert "schema readiness check failed" in str(exc.value).lower()


def test_restore_refused_without_confirmation(populated_db, tmp_path):
    result = backup_database(populated_db["db_file"], populated_db["backup_dir"])
    target_db = tmp_path / "target.sqlite3"

    with pytest.raises(ValueError) as exc:
        restore_database(result["backup_file"], str(target_db), confirm_restore=False)
    assert "--confirm-restore" in str(exc.value)


def test_successful_isolated_restore_and_data_preservation(populated_db, tmp_path):
    backup_result = backup_database(populated_db["db_file"], populated_db["backup_dir"])
    target_db = tmp_path / "target_restore.sqlite3"

    # Pre-create active target database with older state
    engine_target = create_db_engine(f"sqlite:///{target_db}")
    Base.metadata.create_all(engine_target)
    engine_target.dispose()

    # Execute restore with confirmation
    restore_result = restore_database(
        backup_file=backup_result["backup_file"],
        target_db_path=str(target_db),
        confirm_restore=True,
    )
    assert restore_result["status"] == "restored"
    assert restore_result["pre_restore_backup"] is not None
    assert os.path.exists(restore_result["pre_restore_backup"])

    # Verify restored data
    engine_restored = create_db_engine(f"sqlite:///{target_db}")
    session_factory = create_session_factory(engine_restored)
    db = session_factory()
    try:
        loaded = db.query(CapturedEvent).filter_by(event_id="evt-ops-1").first()
        assert loaded is not None
        assert loaded.payload_text == "Data that must survive backup and restore"
    finally:
        db.close()
        engine_restored.dispose()
