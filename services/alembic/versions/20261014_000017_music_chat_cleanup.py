"""Persist safe per-chat cleanup ledger without scanning unknown Telegram history.

Revision ID: 20261014_000017
Revises: 20261009_000016
"""
from alembic import op
import sqlalchemy as sa

revision = "20261014_000017"
down_revision = "20261009_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "music_chat_cleanup_messages" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "music_chat_cleanup_messages",
            sa.Column("chat_id", sa.BigInteger(), primary_key=True),
            sa.Column("message_id", sa.BigInteger(), primary_key=True),
            sa.Column("chat_type", sa.Text(), nullable=False),
            sa.Column("is_audio", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("audio_key", sa.Text()),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_music_chat_cleanup_type_chat", "music_chat_cleanup_messages",
            ["chat_type", "chat_id"],
        )


def downgrade() -> None:
    if "music_chat_cleanup_messages" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_music_chat_cleanup_type_chat", table_name="music_chat_cleanup_messages")
        op.drop_table("music_chat_cleanup_messages")
