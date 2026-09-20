"""004 phase 5 meeting copilot

Revision ID: 004_phase5
Revises: 003_phase2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_phase5"
down_revision: Union[str, None] = "003_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meeting_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("consent_acknowledged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consent_note", sa.Text(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_sessions_lifecycle_status", "meeting_sessions", ["lifecycle_status"])
    op.create_index("ix_meeting_sessions_retention_until", "meeting_sessions", ["retention_until"])
    op.create_table(
        "meeting_transcript_segments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("meeting_session_id", sa.String(length=36), nullable=False),
        sa.Column("speaker_label", sa.String(length=128), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["meeting_session_id"], ["meeting_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_transcript_segments_meeting_session_id", "meeting_transcript_segments", ["meeting_session_id"])
    op.create_table(
        "meeting_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("meeting_session_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_type", sa.String(length=32), nullable=False),
        sa.Column("proposal_text", sa.Text(), nullable=False),
        sa.Column("evidence_segment_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["meeting_session_id"], ["meeting_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_proposals_meeting_session_id", "meeting_proposals", ["meeting_session_id"])
    op.create_index("ix_meeting_proposals_proposal_type", "meeting_proposals", ["proposal_type"])
    op.create_index("ix_meeting_proposals_lifecycle_status", "meeting_proposals", ["lifecycle_status"])


def downgrade() -> None:
    op.drop_table("meeting_proposals")
    op.drop_table("meeting_transcript_segments")
    op.drop_table("meeting_sessions")
