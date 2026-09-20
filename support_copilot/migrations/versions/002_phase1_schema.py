"""002 phase 1 schema

Revision ID: 002_phase1
Revises: 001_initial
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '002_phase1'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. knowledge_articles
    op.create_table(
        'knowledge_articles',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('stable_key', sa.String(length=128), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('product_scope', sa.String(length=64), nullable=False),
        sa.Column('issue_type', sa.String(length=64), nullable=False),
        sa.Column('client_scope', sa.String(length=64), nullable=True),
        sa.Column('lifecycle_status', sa.String(length=32), nullable=False, server_default='draft'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stable_key', name='uq_knowledge_article_stable_key')
    )
    op.create_index('ix_knowledge_articles_stable_key', 'knowledge_articles', ['stable_key'], unique=False)
    op.create_index('ix_knowledge_articles_product_scope', 'knowledge_articles', ['product_scope'], unique=False)
    op.create_index('ix_knowledge_articles_issue_type', 'knowledge_articles', ['issue_type'], unique=False)
    op.create_index('ix_knowledge_articles_client_scope', 'knowledge_articles', ['client_scope'], unique=False)

    # 2. knowledge_article_versions
    op.create_table(
        'knowledge_article_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('article_id', sa.String(length=36), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('immutable_content', sa.Text(), nullable=False),
        sa.Column('required_facts', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('prohibited_claims', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('approval_state', sa.String(length=32), nullable=False, server_default='draft'),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', sa.String(length=128), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['article_id'], ['knowledge_articles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('article_id', 'version', name='uq_article_version')
    )
    op.create_index('ix_knowledge_article_versions_article_id', 'knowledge_article_versions', ['article_id'], unique=False)

    # 3. support_cases
    op.create_table(
        'support_cases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_number', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='open'),
        sa.Column('client_identifier', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('case_number', name='uq_case_number')
    )
    op.create_index('ix_support_cases_case_number', 'support_cases', ['case_number'], unique=False)
    op.create_index('ix_support_cases_status', 'support_cases', ['status'], unique=False)
    op.create_index('ix_support_cases_client_identifier', 'support_cases', ['client_identifier'], unique=False)

    # 4. conversations
    op.create_table(
        'conversations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('external_conversation_id', sa.String(length=128), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['support_cases.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'external_conversation_id', name='uq_conv_provider_ext_id')
    )
    op.create_index('ix_conversations_external_conversation_id', 'conversations', ['external_conversation_id'], unique=False)
    op.create_index('ix_conversations_case_id', 'conversations', ['case_id'], unique=False)

    # 5. response_suggestions
    op.create_table(
        'response_suggestions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('captured_event_id', sa.Integer(), nullable=False),
        sa.Column('resolved_case_id', sa.String(length=36), nullable=True),
        sa.Column('lifecycle_status', sa.String(length=32), nullable=False, server_default='suggested'),
        sa.Column('draft', sa.Text(), nullable=False),
        sa.Column('missing_facts', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('assumptions', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('prohibited_claim_evaluation', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('recommended_action', sa.String(length=128), nullable=False, server_default='review'),
        sa.Column('provider_identifier', sa.String(length=64), nullable=False),
        sa.Column('model_identifier', sa.String(length=64), nullable=True),
        sa.Column('correlation_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('copied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('edited_text_hash', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['captured_event_id'], ['captured_events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resolved_case_id'], ['support_cases.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_response_suggestions_captured_event_id', 'response_suggestions', ['captured_event_id'], unique=False)
    op.create_index('ix_response_suggestions_resolved_case_id', 'response_suggestions', ['resolved_case_id'], unique=False)
    op.create_index('ix_response_suggestions_correlation_id', 'response_suggestions', ['correlation_id'], unique=False)

    # 6. suggestion_sources
    op.create_table(
        'suggestion_sources',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('suggestion_id', sa.String(length=36), nullable=False),
        sa.Column('article_id', sa.String(length=36), nullable=False),
        sa.Column('article_version_id', sa.String(length=36), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('retrieval_rank', sa.Integer(), nullable=False),
        sa.Column('retrieval_score', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['suggestion_id'], ['response_suggestions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['article_id'], ['knowledge_articles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['article_version_id'], ['knowledge_article_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('suggestion_id', 'article_version_id', name='uq_sugg_src_version')
    )
    op.create_index('ix_suggestion_sources_suggestion_id', 'suggestion_sources', ['suggestion_id'], unique=False)
    op.create_index('ix_suggestion_sources_article_id', 'suggestion_sources', ['article_id'], unique=False)
    op.create_index('ix_suggestion_sources_article_version_id', 'suggestion_sources', ['article_version_id'], unique=False)

    # 7. sent_responses
    op.create_table(
        'sent_responses',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('suggestion_id', sa.String(length=36), nullable=False),
        sa.Column('exact_sent_text', sa.Text(), nullable=False),
        sa.Column('final_text_hash', sa.String(length=64), nullable=False),
        sa.Column('confirmed_by_actor', sa.String(length=128), nullable=False),
        sa.Column('confirmation_idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['suggestion_id'], ['response_suggestions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('suggestion_id', name='uq_sent_suggestion'),
        sa.UniqueConstraint('confirmation_idempotency_key', name='uq_sent_idempotency')
    )
    op.create_index('ix_sent_responses_suggestion_id', 'sent_responses', ['suggestion_id'], unique=False)
    op.create_index('ix_sent_responses_confirmation_idempotency_key', 'sent_responses', ['confirmation_idempotency_key'], unique=False)

    # 8. activity_events
    op.create_table(
        'activity_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('subject_type', sa.String(length=32), nullable=False),
        sa.Column('subject_id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=True),
        sa.Column('correlation_id', sa.String(length=128), nullable=False),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('details_json', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_activity_events_event_type', 'activity_events', ['event_type'], unique=False)
    op.create_index('ix_activity_events_subject_id', 'activity_events', ['subject_id'], unique=False)
    op.create_index('ix_activity_events_case_id', 'activity_events', ['case_id'], unique=False)
    op.create_index('ix_activity_events_correlation_id', 'activity_events', ['correlation_id'], unique=False)
    op.create_index('ix_activity_events_actor', 'activity_events', ['actor'], unique=False)

    # 9. SQLite FTS5 table for knowledge articles
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_articles_fts USING fts5("
        "version_id UNINDEXED, "
        "article_id UNINDEXED, "
        "title, "
        "content, "
        "product_scope, "
        "issue_type, "
        "client_scope"
        ");"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_articles_fts;")
    op.drop_table('activity_events')
    op.drop_table('sent_responses')
    op.drop_table('suggestion_sources')
    op.drop_table('response_suggestions')
    op.drop_table('conversations')
    op.drop_table('support_cases')
    op.drop_table('knowledge_article_versions')
    op.drop_table('knowledge_articles')
