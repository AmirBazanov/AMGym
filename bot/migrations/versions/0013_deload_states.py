"""auto deload state

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('deload_states',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('started_on', sa.Date(), nullable=True),
    sa.Column('until', sa.Date(), nullable=True),
    sa.Column('ask_after', sa.DateTime(timezone=True), nullable=True),
    sa.Column('offered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_deload_states_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_deload_states'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('deload_states')
