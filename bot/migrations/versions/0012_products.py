"""packaged products

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('brand', sa.String(length=200), nullable=True),
    sa.Column('barcode', sa.String(length=32), nullable=True),
    sa.Column('kcal_100g', sa.Numeric(precision=6, scale=1), nullable=False),
    sa.Column('protein_100g', sa.Numeric(precision=5, scale=1), nullable=False),
    sa.Column('fat_100g', sa.Numeric(precision=5, scale=1), nullable=False),
    sa.Column('carbs_100g', sa.Numeric(precision=5, scale=1), nullable=False),
    sa.Column('net_weight_g', sa.Numeric(precision=7, scale=1), nullable=True),
    sa.Column('serving_g', sa.Numeric(precision=6, scale=1), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('aliases', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_products_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products')),
    sa.UniqueConstraint('user_id', 'barcode', name=op.f('uq_products_user_id'))
    )
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_products_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_products_user_id'))

    op.drop_table('products')
