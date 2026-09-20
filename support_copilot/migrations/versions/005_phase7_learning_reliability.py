"""005 phase 7 response learning and reliability

Revision ID: 005_phase7
Revises: 004_phase5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_phase7"
down_revision: Union[str, None] = "004_phase5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "response_learning_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("sent_response_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_title", sa.String(length=256), nullable=False),
        sa.Column("candidate_content", sa.Text(), nullable=False),
        sa.Column("product_scope", sa.String(length=64), nullable=False),
        sa.Column("issue_type", sa.String(length=64), nullable=False),
        sa.Column("client_scope", sa.String(length=64), nullable=True),
        sa.Column("target_stable_key", sa.String(length=128), nullable=True),
        sa.Column("target_article_id", sa.String(length=36), nullable=True),
        sa.Column("source_versions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_article_version_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["suggestion_id"], ["response_suggestions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sent_response_id"], ["sent_responses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_article_id"], ["knowledge_articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resulting_article_version_id"], ["knowledge_article_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_response_learning_candidates_suggestion_id", "response_learning_candidates", ["suggestion_id"])
    op.create_index("ix_response_learning_candidates_sent_response_id", "response_learning_candidates", ["sent_response_id"])
    op.create_index("ix_response_learning_candidates_product_scope", "response_learning_candidates", ["product_scope"])
    op.create_index("ix_response_learning_candidates_issue_type", "response_learning_candidates", ["issue_type"])
    op.create_index("ix_response_learning_candidates_lifecycle_status", "response_learning_candidates", ["lifecycle_status"])


def downgrade() -> None:
    op.drop_table("response_learning_candidates")
