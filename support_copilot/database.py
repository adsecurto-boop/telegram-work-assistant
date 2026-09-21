import os
from sqlalchemy import create_engine, inspect, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from .config import Settings, get_settings
from .logger import logger

def create_db_engine(db_path: str) -> Engine:
    connect_args = {"check_same_thread": False, "timeout": 30.0} if "sqlite" in db_path else {}
    eng = create_engine(db_path, connect_args=connect_args)

    if "sqlite" in db_path:
        @event.listens_for(eng, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    return eng

def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)

REQUIRED_TABLES_PHASE0 = {"captured_events", "integration_idempotency", "audit_events"}
CURRENT_SCHEMA_REVISION = "005_phase7"

REQUIRED_TABLES_PHASE1 = REQUIRED_TABLES_PHASE0 | {
    "knowledge_articles",
    "knowledge_article_versions",
    "support_cases",
    "conversations",
    "response_suggestions",
    "suggestion_sources",
    "sent_responses",
    "activity_events",
}
REQUIRED_TABLES_PHASE2 = REQUIRED_TABLES_PHASE1 | {"report_snapshots"}
REQUIRED_TABLES_PHASE5 = REQUIRED_TABLES_PHASE2 | {
    "meeting_sessions",
    "meeting_transcript_segments",
    "meeting_proposals",
}
REQUIRED_TABLES_PHASE7 = REQUIRED_TABLES_PHASE5 | {"response_learning_candidates"}
REQUIRED_TABLES_CURRENT = REQUIRED_TABLES_PHASE7 | {"knowledge_articles_fts"}

def verify_schema_readiness(engine: Engine, required_tables: set = None) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if required_tables is None:
        if "response_learning_candidates" in existing_tables:
            required_tables = REQUIRED_TABLES_PHASE7
        elif "meeting_sessions" in existing_tables:
            required_tables = REQUIRED_TABLES_PHASE5
        elif "report_snapshots" in existing_tables:
            required_tables = REQUIRED_TABLES_PHASE2
        elif "knowledge_articles" in existing_tables:
            required_tables = REQUIRED_TABLES_PHASE1
        else:
            required_tables = REQUIRED_TABLES_PHASE0
    missing = required_tables - existing_tables
    if missing:
        raise RuntimeError(
            f"Database schema is not ready. Missing required tables: {', '.join(sorted(missing))}. "
            f"Run Alembic migrations before starting the application."
        )
