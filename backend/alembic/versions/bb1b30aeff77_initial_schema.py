"""initial schema — all tables, constraints, indexes, and FTS5

Revision ID: bb1b30aeff77
Revises:
Create Date: 2026-06-23

This is the initial migration. It creates every table defined in 
``app.database.models`` from scratch.  New project databases are 
built by ``alembic upgrade head``  (there is no longer a separate
``Base.metadata.create_all`` path).  All foreign keys use ``ON DELETE CASCADE``
so deleting a session/annotation automatically removes its children.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb1b30aeff77'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('annotations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('file_path', sa.String(length=500), nullable=False),
    sa.Column('from_pos', sa.Integer(), nullable=False),
    sa.Column('to_pos', sa.Integer(), nullable=False),
    sa.Column('original_text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_annotations'))
    )
    op.create_index(op.f('ix_annotations_file_path'), 'annotations', ['file_path'], unique=False)

    op.create_table('background_tasks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('queue', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('payload_json', sa.Text(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=True),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('lease_owner', sa.String(length=100), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_background_tasks'))
    )
    op.create_index(op.f('ix_background_tasks_dedupe_key'), 'background_tasks', ['dedupe_key'], unique=False)
    op.create_index(op.f('ix_background_tasks_kind'), 'background_tasks', ['kind'], unique=False)
    op.create_index(op.f('ix_background_tasks_project_id'), 'background_tasks', ['project_id'], unique=False)
    op.create_index(op.f('ix_background_tasks_queue'), 'background_tasks', ['queue'], unique=False)
    op.create_index(op.f('ix_background_tasks_status'), 'background_tasks', ['status'], unique=False)

    op.create_table('library_documents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=500), nullable=True),
    sa.Column('doc_type', sa.String(length=50), nullable=False),
    sa.Column('keywords', sa.Text(), nullable=True),
    sa.Column('embedding_id', sa.String(length=100), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('processing_status', sa.String(length=20), nullable=False),
    sa.Column('processing_log', sa.Text(), nullable=False),
    sa.Column('processing_started_at', sa.DateTime(), nullable=True),
    sa.Column('processing_completed_at', sa.DateTime(), nullable=True),
    sa.Column('file_name', sa.String(length=500), nullable=True),
    sa.Column('file_path', sa.String(length=1000), nullable=True),
    sa.Column('parent_id', sa.String(length=36), nullable=True),
    sa.Column('is_folder', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['library_documents.id'], name=op.f('fk_library_documents_parent_id_library_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_library_documents'))
    )
    op.create_index(op.f('ix_library_documents_parent_id'), 'library_documents', ['parent_id'], unique=False)

    op.create_table('project_config',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('value', sa.Text(), server_default='', nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_project_config'))
    )
    op.create_table('sessions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('session_kind', sa.String(length=20), nullable=False),
    sa.Column('agent_type', sa.String(length=20), nullable=True),
    sa.Column('parent_session_id', sa.String(length=36), nullable=True),
    sa.Column('parent_tool_call_id', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('is_archived', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['parent_session_id'], ['sessions.id'], name=op.f('fk_sessions_parent_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions'))
    )
    op.create_index(op.f('ix_sessions_parent_session_id'), 'sessions', ['parent_session_id'], unique=False)
    op.create_index(op.f('ix_sessions_project_id'), 'sessions', ['project_id'], unique=False)

    op.create_table('task_state',
    sa.Column('task_id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=True),
    sa.Column('owner_type', sa.String(length=50), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('task_type', sa.String(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('heartbeat_at', sa.String(), nullable=True),
    sa.Column('interaction_state', sa.Text(), nullable=True),
    sa.Column('created_at', sa.String(), nullable=False),
    sa.Column('updated_at', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', name=op.f('pk_task_state'))
    )
    op.create_index(op.f('ix_task_state_owner_id'), 'task_state', ['owner_id'], unique=False)
    op.create_index(op.f('ix_task_state_owner_type'), 'task_state', ['owner_type'], unique=False)
    op.create_index(op.f('ix_task_state_session_id'), 'task_state', ['session_id'], unique=False)

    op.create_table('messages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=True),
    sa.Column('annotation_id', sa.String(length=36), nullable=True),
    sa.Column('role', sa.String(length=50), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tool_calls', sa.Text(), nullable=True),
    sa.Column('tool_call_id', sa.String(length=100), nullable=True),
    sa.Column('reasoning_content', sa.Text(), nullable=True),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('is_boundary', sa.Boolean(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('\n            (session_id IS NOT NULL AND annotation_id IS NULL)\n            OR (session_id IS NULL AND annotation_id IS NOT NULL)\n            ', name='ck_message_one_owner'),
    sa.ForeignKeyConstraint(['annotation_id'], ['annotations.id'], name=op.f('fk_messages_annotation_id_annotations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], name=op.f('fk_messages_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('annotation_id', 'seq', name='uq_message_annotation_seq'),
    sa.UniqueConstraint('session_id', 'seq', name='uq_message_session_seq')
    )
    op.create_index(op.f('ix_messages_annotation_id'), 'messages', ['annotation_id'], unique=False)
    op.create_index(op.f('ix_messages_session_id'), 'messages', ['session_id'], unique=False)

    op.create_table('tasks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=False),
    sa.Column('subject', sa.String(length=500), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('metadata_json', sa.Text(), nullable=True),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], name=op.f('fk_tasks_session_id_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks')),
    sa.UniqueConstraint('session_id', 'seq', name='uq_task_session_seq')
    )
    op.create_index(op.f('ix_tasks_session_id'), 'tasks', ['session_id'], unique=False)

    # ── FTS5 full-text search index for library_documents ──
    # The virtual table and its sync triggers are part of the schema, not
    # an application-layer concern.  Any future table rebuild for
    # library_documents must recreate these triggers afterwards.
    op.execute("""
        CREATE VIRTUAL TABLE library_documents_fts
        USING fts5(title, description, content,
                   content='library_documents', content_rowid='rowid',
                   tokenize='trigram')
    """)
    op.execute("""
        CREATE TRIGGER library_documents_ai AFTER INSERT ON library_documents BEGIN
            INSERT INTO library_documents_fts(rowid, title, description, content)
            VALUES (new.rowid, new.title, new.description, new.content);
        END
    """)
    op.execute("""
        CREATE TRIGGER library_documents_ad AFTER DELETE ON library_documents BEGIN
            INSERT INTO library_documents_fts(library_documents_fts, rowid, title, description, content)
            VALUES ('delete', old.rowid, old.title, old.description, old.content);
        END
    """)
    op.execute("""
        CREATE TRIGGER library_documents_au AFTER UPDATE ON library_documents BEGIN
            INSERT INTO library_documents_fts(library_documents_fts, rowid, title, description, content)
            VALUES ('delete', old.rowid, old.title, old.description, old.content);
            INSERT INTO library_documents_fts(rowid, title, description, content)
            VALUES (new.rowid, new.title, new.description, new.content);
        END
    """)


def downgrade() -> None:
    raise NotImplementedError(
        "cannot downgrade past the initial migration; data loss would result. "
        "Restore from a backup instead."
    )
