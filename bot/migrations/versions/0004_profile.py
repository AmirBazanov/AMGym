"""profile for AI advice, weekly reminders

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weight_kg', sa.Numeric(precision=5, scale=1), nullable=True))
        batch_op.add_column(sa.Column('height_cm', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('birth_year', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('goal', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('about', sa.Text(), nullable=True))
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weekday', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_column('weekday')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('about')
        batch_op.drop_column('goal')
        batch_op.drop_column('birth_year')
        batch_op.drop_column('height_cm')
        batch_op.drop_column('weight_kg')
