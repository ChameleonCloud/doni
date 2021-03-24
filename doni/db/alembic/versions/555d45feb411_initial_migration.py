"""initial migration

Revision ID: 555d45feb411
Revises:
Create Date: 2021-03-08 19:55:08.497100

"""
import sqlalchemy as sa
from alembic import op
from oslo_db.sqlalchemy import types as oslo_sa_types

# revision identifiers, used by Alembic.
revision = "555d45feb411"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hardware",
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("hardware_type", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("properties", oslo_sa_types.JsonEncodedDict(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uniq_hardware0name"),
        sa.UniqueConstraint("uuid", name="uniq_hardware0uuid"),
    )
    op.create_table(
        "availability_window",
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=True),
        sa.Column("hardware_uuid", sa.String(length=36), nullable=True),
        sa.Column("start", sa.DateTime(), nullable=True),
        sa.Column("end", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["hardware_uuid"],
            ["hardware.uuid"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "availability_window_hardware_uuid_idx",
        "availability_window",
        ["hardware_uuid"],
        unique=False,
    )
    op.create_table(
        "worker_task",
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=True),
        sa.Column("hardware_uuid", sa.String(length=36), nullable=True),
        sa.Column("worker_type", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=15), nullable=True),
        sa.Column("state_details", oslo_sa_types.JsonEncodedDict(), nullable=True),
        sa.ForeignKeyConstraint(
            ["hardware_uuid"],
            ["hardware.uuid"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "hardware_uuid",
            "worker_type",
            name="uniq_workers0hardware_uuid0worker_type",
        ),
        sa.UniqueConstraint("uuid", name="uniq_workers0uuid"),
    )


def downgrade():
    op.drop_table("worker_task")
    op.drop_index(
        "availability_window_hardware_uuid_idx", table_name="availability_window"
    )
    op.drop_table("availability_window")
    op.drop_table("hardware")
