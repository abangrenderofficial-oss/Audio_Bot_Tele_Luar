"""Track group messages that /clearall may safely delete.

Revision ID: 20261006_000015
Revises: 20261006_000014
Create Date: 2026-10-06 18:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261006_000015"
down_revision: Union[str, Sequence[str], None] = "20261006_000014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "music_group_cleanup_messages" not in tables:
        op.create_table(
            "music_group_cleanup_messages",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "group_id",
                sa.BigInteger(),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "kind",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'bot_text'"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "group_id",
                "message_id",
                name="uq_music_group_cleanup_group_message",
            ),
        )
        op.create_index(
            "ix_music_group_cleanup_group_created",
            "music_group_cleanup_messages",
            ["group_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "music_group_cleanup_messages" in tables:
        op.drop_index(
            "ix_music_group_cleanup_group_created",
            table_name="music_group_cleanup_messages",
        )
        op.drop_table("music_group_cleanup_messages")
