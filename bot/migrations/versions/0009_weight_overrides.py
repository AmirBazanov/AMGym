"""weights for today set from the chat

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('weight_overrides',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('exercise_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('weight_kg', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_weight_overrides_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['exercise_id'], ['exercises.id'], name=op.f('fk_weight_overrides_exercise_id_exercises')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weight_overrides')),
    sa.UniqueConstraint('user_id', 'exercise_id', 'day', name=op.f('uq_weight_overrides_user_id'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('weight_overrides')
