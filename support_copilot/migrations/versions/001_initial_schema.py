"""001 initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-09-20 23:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        'captured_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor_id', sa.String(length=128), nullable=False),
        sa.Column('actor_role', sa.String(length=32), nullable=False),
        sa.Column('conversation_id', sa.String(length=128), nullable=False),
        sa.Column('case_hint', sa.String(length=128), nullable=True),
        sa.Column('payload_text', sa.Text(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('correlation_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'event_id', name='uq_captured_provider_event')
    )
    op.create_index('ix_captured_events_provider', 'captured_events', ['provider'], unique=False)
    op.create_index('ix_captured_events_event_id', 'captured_events', ['event_id'], unique=False)
    op.create_index('ix_captured_events_conversation_id', 'captured_events', ['conversation_id'], unique=False)
    op.create_index('ix_captured_events_correlation_id', 'captured_events', ['correlation_id'], unique=False)

    op.create_table(
        'integration_idempotency',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('correlation_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('response_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'event_id', name='uq_idempotency_provider_event')
    )
    op.create_index('ix_integration_idempotency_provider', 'integration_idempotency', ['provider'], unique=False)
    op.create_index('ix_integration_idempotency_event_id', 'integration_idempotency', ['event_id'], unique=False)
    op.create_index('ix_integration_idempotency_correlation_id', 'integration_idempotency', ['correlation_id'], unique=False)

    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('resource', sa.String(length=128), nullable=False),
        sa.Column('correlation_id', sa.String(length=128), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('details_json', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_events_actor', 'audit_events', ['actor'], unique=False)
    op.create_index('ix_audit_events_correlation_id', 'audit_events', ['correlation_id'], unique=False)

def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('integration_idempotency')
    op.drop_table('captured_events')
