"""Add persistent group music playlist state.

Revision ID: 20261006_000014
Revises: 20260914_000013
Create Date: 2026-10-06 13:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261006_000014"
down_revision: Union[str, Sequence[str], None] = "20260914_000013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "music_group_settings" not in tables:
        op.create_table(
            "music_group_settings",
            sa.Column(
                "group_id",
                sa.BigInteger(),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                primary_key=True,
                autoincrement=False,
            ),
            sa.Column(
                "connected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("connected_by_user_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "connected_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
        )

    if "music_group_tracks" not in tables:
        op.create_table(
            "music_group_tracks",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "group_id",
                sa.BigInteger(),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("added_by_user_id", sa.BigInteger(), nullable=True),
            sa.Column("service", sa.Text(), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("performer", sa.Text(), nullable=True),
            sa.Column("telegram_file_id", sa.Text(), nullable=False),
            sa.Column("duration_seconds", sa.Float(), nullable=True),
            sa.Column("source_message_id", sa.BigInteger(), nullable=True),
            sa.Column("audio_message_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "group_id",
                "audio_message_id",
                name="uq_music_group_tracks_group_audio_message",
            ),
        )
        op.create_index(
            "ix_music_group_tracks_group_created",
            "music_group_tracks",
            ["group_id", "created_at"],
        )
        op.create_index(
            "ix_music_group_tracks_group_source_message",
            "music_group_tracks",
            ["group_id", "source_message_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "music_group_tracks" in tables:
        op.drop_index(
            "ix_music_group_tracks_group_source_message",
            table_name="music_group_tracks",
        )
        op.drop_index(
            "ix_music_group_tracks_group_created",
            table_name="music_group_tracks",
        )
        op.drop_table("music_group_tracks")

    if "music_group_settings" in tables:
        op.drop_table("music_group_settings")
