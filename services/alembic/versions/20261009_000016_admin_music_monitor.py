"""Persist the connected admin audio monitor group across bot restarts.

Revision ID: 20261009_000016
Revises: 20261006_000015
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261009_000016"
down_revision: Union[str, Sequence[str], None] = "20261006_000015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "admin_music_monitor_settings" not in inspector.get_table_names():
        op.create_table(
            "admin_music_monitor_settings",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("group_id", sa.BigInteger(), nullable=False),
            sa.Column("group_title", sa.Text(), nullable=True),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "admin_music_monitor_settings" in inspector.get_table_names():
        op.drop_table("admin_music_monitor_settings")
