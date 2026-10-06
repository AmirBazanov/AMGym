"""wellbeing entries (sleep, pains, energy, mood)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('wellbeing_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('noted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sleep_hours', sa.Numeric(precision=3, scale=1), nullable=True),
    sa.Column('sleep_quality', sa.Integer(), nullable=True),
    sa.Column('energy', sa.Integer(), nullable=True),
    sa.Column('mood', sa.Integer(), nullable=True),
    sa.Column('pains', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_wellbeing_entries_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wellbeing_entries'))
    )
    with op.batch_alter_table('wellbeing_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wellbeing_entries_noted_at'), ['noted_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_wellbeing_entries_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('wellbeing_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wellbeing_entries_user_id'))
        batch_op.drop_index(batch_op.f('ix_wellbeing_entries_noted_at'))

    op.drop_table('wellbeing_entries')
