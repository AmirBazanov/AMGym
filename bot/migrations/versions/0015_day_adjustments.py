"""day adjustments

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-09 05:26:22.080292

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('day_adjustments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('weight_factor', sa.Numeric(precision=4, scale=2), nullable=True),
    sa.Column('sets_delta', sa.Integer(), nullable=True),
    sa.Column('skip_json', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_day_adjustments_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_day_adjustments')),
    sa.UniqueConstraint('user_id', 'day', name=op.f('uq_day_adjustments_user_id'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('day_adjustments')
