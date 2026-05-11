"""add observed_state to worker_task

Revision ID: e58738bfc58e
Revises: 868606f1faff
Create Date: 2026-05-11 15:05:22.211899
"""

from alembic import op
import sqlalchemy as sa
from oslo_db.sqlalchemy import types as oslo_sa_types


# revision identifiers, used by Alembic.
revision = "e58738bfc58e"
down_revision = "868606f1faff"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "worker_task",
        sa.Column("observed_state", oslo_sa_types.JsonEncodedDict(), nullable=True),
    )


def downgrade():
    op.drop_column("worker_task", "observed_state")
