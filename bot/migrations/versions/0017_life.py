"""life: «Дела» tables and the owner's reminder/summary settings

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('life_chores',
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('every_days', sa.Integer(), nullable=False),
    sa.Column('last_done', sa.Date(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_chores_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_chores')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_chores_user_id'))
    )
    with op.batch_alter_table('life_chores', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_chores_user_id'), ['user_id'], unique=False)

    op.create_table('life_projects',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('link', sa.String(length=500), nullable=True),
    sa.Column('next_step', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_projects_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_projects')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_projects_user_id'))
    )
    with op.batch_alter_table('life_projects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_projects_user_id'), ['user_id'], unique=False)

    op.create_table('life_slots',
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.Column('start', sa.String(length=5), nullable=False),
    sa.Column('end', sa.String(length=5), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('area', sa.String(length=16), nullable=False),
    sa.Column('tag', sa.String(length=32), nullable=True),
    sa.Column('place', sa.String(length=200), nullable=True),
    sa.Column('vld', sa.String(length=5), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_slots_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_slots')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_slots_user_id'))
    )
    with op.batch_alter_table('life_slots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_slots_user_id'), ['user_id'], unique=False)

    op.create_table('life_deadlines',
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('course', sa.String(length=200), nullable=True),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminded_on', sa.Date(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['life_projects.id'], name=op.f('fk_life_deadlines_project_id_life_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_deadlines_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_deadlines')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_deadlines_user_id'))
    )
    with op.batch_alter_table('life_deadlines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_deadlines_user_id'), ['user_id'], unique=False)

    op.create_table('life_events',
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('start', sa.String(length=5), nullable=True),
    sa.Column('end', sa.String(length=5), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('area', sa.String(length=16), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('remind_before', sa.Integer(), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['life_projects.id'], name=op.f('fk_life_events_project_id_life_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_events_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_events')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_events_user_id'))
    )
    with op.batch_alter_table('life_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_events_user_id'), ['user_id'], unique=False)

    op.create_table('life_notes',
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('author', sa.String(length=8), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['life_projects.id'], name=op.f('fk_life_notes_project_id_life_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_notes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_notes')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_notes_user_id'))
    )
    with op.batch_alter_table('life_notes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_notes_user_id'), ['user_id'], unique=False)

    op.create_table('life_tasks',
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('tag', sa.String(length=32), nullable=True),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['life_projects.id'], name=op.f('fk_life_tasks_project_id_life_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_life_tasks_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_life_tasks')),
    sa.UniqueConstraint('user_id', 'source', 'external_id', name=op.f('uq_life_tasks_user_id'))
    )
    with op.batch_alter_table('life_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_life_tasks_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('life_remind_before', sa.Integer(), server_default='30', nullable=False))
        batch_op.add_column(sa.Column('life_summary_minute', sa.Integer(), server_default='480', nullable=False))
        batch_op.add_column(sa.Column('life_summary_enabled', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('life_summary_sent_on', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('life_imported_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('life_imported_at')
        batch_op.drop_column('life_summary_sent_on')
        batch_op.drop_column('life_summary_enabled')
        batch_op.drop_column('life_summary_minute')
        batch_op.drop_column('life_remind_before')

    with op.batch_alter_table('life_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_tasks_user_id'))

    op.drop_table('life_tasks')
    with op.batch_alter_table('life_notes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_notes_user_id'))

    op.drop_table('life_notes')
    with op.batch_alter_table('life_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_events_user_id'))

    op.drop_table('life_events')
    with op.batch_alter_table('life_deadlines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_deadlines_user_id'))

    op.drop_table('life_deadlines')
    with op.batch_alter_table('life_slots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_slots_user_id'))

    op.drop_table('life_slots')
    with op.batch_alter_table('life_projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_projects_user_id'))

    op.drop_table('life_projects')
    with op.batch_alter_table('life_chores', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_life_chores_user_id'))

    op.drop_table('life_chores')
