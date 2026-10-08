"""program editor: own copies of programs, day focus, prescription snapshot in workouts

Downgrade is lossy and not a true inverse:
- users' own copies stay as ordinary programs without an owner (visible to everyone as templates), and
  their day focus, base_day_id and version are gone;
- `workouts.targets_json` is KEPT on downgrade (0013 code ignores the extra nullable column). A later
  upgrade only fills rows where it is NULL: rebuilding snapshots from day items would rewrite the history
  of workouts whose (copy) day was edited after they were saved.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-08 22:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _target(sets, reps_min, reps_max, drop_reps) -> tuple[str, bool]:
    """Frozen copy of workouts._item_target as of 0013: what the history showed before this migration.
    Never import the app's raw_prescription here, a later change to it must not rewrite old backfills."""
    if isinstance(drop_reps, str):  # JSON column read through raw SQL: text on SQLite, a list on Postgres
        drop_reps = json.loads(drop_reps)
    if drop_reps:
        return f"дропсет {sets}х {'-'.join(map(str, drop_reps))}", True
    return f"{sets}х{reps_min}-{reps_max}", False


def _backfill_targets() -> None:
    conn = op.get_bind()
    day_ids = [
        r[0] for r in conn.execute(
            sa.text("SELECT DISTINCT program_day_id FROM workouts WHERE program_day_id IS NOT NULL")
        )
    ]
    for day_id in day_ids:
        items = conn.execute(
            sa.text(
                'SELECT exercise_id, sets, reps_min, reps_max, drop_reps FROM program_items '
                'WHERE day_id = :d ORDER BY "order", id'
            ),
            {"d": day_id},
        ).all()
        snapshot = []
        for exercise_id, sets, reps_min, reps_max, drop_reps in items:
            target, dropset = _target(sets, reps_min, reps_max, drop_reps)
            snapshot.append({"exerciseId": exercise_id, "target": target, "dropset": dropset})
        conn.execute(  # rows that kept their snapshot through a downgrade are never rebuilt
            sa.text("UPDATE workouts SET targets_json = :t WHERE program_day_id = :d AND targets_json IS NULL"),
            {"t": json.dumps(snapshot, ensure_ascii=False), "d": day_id},
        )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('programs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('based_on_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))
        batch_op.create_foreign_key(
            batch_op.f('fk_programs_owner_user_id_users'), 'users', ['owner_user_id'], ['id'], ondelete='CASCADE'
        )
        batch_op.create_foreign_key(
            batch_op.f('fk_programs_based_on_id_programs'), 'programs', ['based_on_id'], ['id'], ondelete='SET NULL'
        )

    with op.batch_alter_table('program_days', schema=None) as batch_op:
        batch_op.add_column(sa.Column('focus', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('base_day_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_program_days_base_day_id_program_days'), 'program_days', ['base_day_id'], ['id'],
            ondelete='SET NULL',
        )

    # The column survives a downgrade (see the module doc): add it only when it is not there yet.
    if 'targets_json' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('workouts')}:
        with op.batch_alter_table('workouts', schema=None) as batch_op:
            batch_op.add_column(sa.Column('targets_json', sa.Text(), nullable=True))

    _backfill_targets()


def downgrade() -> None:
    """Downgrade schema. Lossy (see the module doc): own copies stay as ordinary programs without an owner;
    workouts.targets_json is kept on purpose, so the history snapshots survive a downgrade/upgrade."""
    with op.batch_alter_table('program_days', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_program_days_base_day_id_program_days'), type_='foreignkey')
        batch_op.drop_column('base_day_id')
        batch_op.drop_column('focus')

    with op.batch_alter_table('programs', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_programs_based_on_id_programs'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_programs_owner_user_id_users'), type_='foreignkey')
        batch_op.drop_column('version')
        batch_op.drop_column('based_on_id')
        batch_op.drop_column('owner_user_id')
