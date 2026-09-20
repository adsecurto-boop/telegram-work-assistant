"""003 phase 2 daily memory and reports

Revision ID: 003_phase2
Revises: 002_phase1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_phase2"
down_revision: Union[str, None] = "002_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("report_type", sa.String(length=16), nullable=False),
        sa.Column("report_date", sa.String(length=10), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="preview"),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_snapshots_report_type", "report_snapshots", ["report_type"])
    op.create_index("ix_report_snapshots_report_date", "report_snapshots", ["report_date"])
    op.create_index("ix_report_snapshots_lifecycle_status", "report_snapshots", ["lifecycle_status"])


def downgrade() -> None:
    op.drop_table("report_snapshots")
