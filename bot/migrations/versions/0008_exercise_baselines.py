"""working weights from user facts

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('exercise_baselines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('fact_id', sa.Integer(), nullable=False),
    sa.Column('exercise_id', sa.Integer(), nullable=False),
    sa.Column('weight_kg', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('reps', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_exercise_baselines_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['fact_id'], ['user_facts.id'], name=op.f('fk_exercise_baselines_fact_id_user_facts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['exercise_id'], ['exercises.id'], name=op.f('fk_exercise_baselines_exercise_id_exercises')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exercise_baselines'))
    )
    with op.batch_alter_table('exercise_baselines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exercise_baselines_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exercise_baselines_fact_id'), ['fact_id'], unique=False)
    with op.batch_alter_table('user_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('baselines_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_facts', schema=None) as batch_op:
        batch_op.drop_column('baselines_at')
    with op.batch_alter_table('exercise_baselines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exercise_baselines_fact_id'))
        batch_op.drop_index(batch_op.f('ix_exercise_baselines_user_id'))

    op.drop_table('exercise_baselines')
