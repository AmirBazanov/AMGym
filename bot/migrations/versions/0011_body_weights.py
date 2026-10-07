"""body weight measurements

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('body_weights',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('measured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('weight_kg', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('raw_text', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_body_weights_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_body_weights')),
    sa.UniqueConstraint('user_id', 'day', name=op.f('uq_body_weights_user_id'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('body_weights')
