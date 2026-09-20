import os
import sqlite3
import pytest
from datetime import datetime, timezone
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from support_copilot.config import Settings
from support_copilot.database import (
    create_db_engine,
    verify_schema_readiness,
    REQUIRED_TABLES_PHASE0,
    REQUIRED_TABLES_PHASE1,
    REQUIRED_TABLES_PHASE2,
)
from support_copilot.migration_runner import MigrationRunner, MigrationError
from support_copilot.models import CapturedEvent


def test_phase1_migration_upgrade_and_schema_readiness(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "mig_phase1.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{db_file}")
    runner = MigrationRunner(settings)

    # 1. Upgrade to head (which includes 001 and 002)
    runner.run_upgrade("head")

    engine = create_db_engine(f"sqlite:///{db_file}")
    try:
        # Verifies all Phase 0 + Phase 1 tables exist
        verify_schema_readiness(engine, required_tables=REQUIRED_TABLES_PHASE1)
        verify_schema_readiness(engine, required_tables=REQUIRED_TABLES_PHASE2)

        # Verify FTS5 virtual table exists
        conn = sqlite3.connect(str(db_file))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_articles_fts';")
            row = cursor.fetchone()
            assert row is not None, "knowledge_articles_fts virtual table must exist"
        finally:
            conn.close()
    finally:
        engine.dispose()


def test_migration_preserves_phase0_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "preserve.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{db_file}")
    runner = MigrationRunner(settings)

    # Migrate to Phase 0 initial schema first
    runner.run_upgrade("001_initial")

    # Insert a Phase 0 captured event
    engine = create_db_engine(f"sqlite:///{db_file}")
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        event = CapturedEvent(
            provider="manual",
            event_id="evt-preserve-test",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-test",
            actor_role="client",
            conversation_id="conv-preserve",
            payload_text="Original Phase 0 payload",
            schema_version=1,
            correlation_id="corr-preserve-001",
        )
        db.add(event)
        db.commit()
    finally:
        db.close()
        engine.dispose()

    # Migrate to Phase 1 head
    runner.run_upgrade("head")

    # Verify Phase 0 data survived intact
    engine2 = create_db_engine(f"sqlite:///{db_file}")
    session_factory2 = sessionmaker(bind=engine2)
    db2 = session_factory2()
    try:
        loaded = db2.query(CapturedEvent).filter_by(event_id="evt-preserve-test").first()
        assert loaded is not None
        assert loaded.payload_text == "Original Phase 0 payload"
        assert loaded.correlation_id == "corr-preserve-001"
    finally:
        db2.close()
        engine2.dispose()


def test_downgrade_to_phase0_preserves_phase0_data_and_drops_phase1_tables(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "downgrade_test.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{db_file}")
    runner = MigrationRunner(settings)

    # Upgrade to head
    runner.run_upgrade("head")

    # Insert Phase 0 data
    engine = create_db_engine(f"sqlite:///{db_file}")
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        event = CapturedEvent(
            provider="manual",
            event_id="evt-downgrade-preserve",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-test",
            actor_role="client",
            conversation_id="conv-downgrade",
            payload_text="Survives downgrade",
            schema_version=1,
            correlation_id="corr-downgrade-001",
        )
        db.add(event)
        db.commit()
    finally:
        db.close()
        engine.dispose()

    # Downgrade to 001_initial requires explicit authorization
    with pytest.raises(MigrationError) as exc_info:
        runner.run_downgrade("001_initial", allow_downgrade=False)
    assert "--allow-downgrade" in str(exc_info.value)

    # Downgrade with authorization
    backup = runner.run_downgrade("001_initial", allow_downgrade=True)
    assert backup != ""
    assert os.path.exists(backup)

    engine2 = create_db_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine2)
        tables = set(inspector.get_table_names())
        # Phase 0 tables remain
        assert REQUIRED_TABLES_PHASE0.issubset(tables)
        # Phase 1 tables are gone
        phase1_only = REQUIRED_TABLES_PHASE1 - REQUIRED_TABLES_PHASE0
        assert not tables.intersection(phase1_only)

        # Phase 0 data intact
        session_factory2 = sessionmaker(bind=engine2)
        db2 = session_factory2()
        try:
            loaded = db2.query(CapturedEvent).filter_by(event_id="evt-downgrade-preserve").first()
            assert loaded is not None
            assert loaded.payload_text == "Survives downgrade"
        finally:
            db2.close()
    finally:
        engine2.dispose()
