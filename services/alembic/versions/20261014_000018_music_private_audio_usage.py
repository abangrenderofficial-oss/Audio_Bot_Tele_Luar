"""Count distinct successful MusiX private audio users, independent of MediaX.

Revision ID: 20261014_000018
Revises: 20261014_000017
"""
from alembic import op
import sqlalchemy as sa

revision = "20261014_000018"
down_revision = "20261014_000017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "music_private_audio_deliveries" not in inspector.get_table_names():
        op.create_table(
            "music_private_audio_deliveries",
            sa.Column("user_id", sa.BigInteger(), primary_key=True),
            sa.Column("audio_message_id", sa.BigInteger(), primary_key=True),
            sa.Column("platform", sa.Text(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                      server_default=sa.func.now()),
        )
        op.create_index(
            "ix_music_private_audio_delivery_created",
            "music_private_audio_deliveries",
            ["created_at"],
        )


def downgrade() -> None:
    if "music_private_audio_deliveries" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index(
            "ix_music_private_audio_delivery_created",
            table_name="music_private_audio_deliveries",
        )
        op.drop_table("music_private_audio_deliveries")
