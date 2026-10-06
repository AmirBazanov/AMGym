"""adaptive day plans

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('day_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('plan_date', sa.Date(), nullable=False),
    sa.Column('readiness', sa.String(length=8), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('exercises_json', sa.Text(), nullable=False),
    sa.Column('inputs_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_day_plans_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_day_plans')),
    sa.UniqueConstraint('user_id', 'plan_date', name=op.f('uq_day_plans_user_id'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('day_plans')
