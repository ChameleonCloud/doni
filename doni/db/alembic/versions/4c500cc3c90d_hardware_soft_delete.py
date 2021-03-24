"""hardware_soft_delete

Revision ID: 4c500cc3c90d
Revises: 8e1686ce62a0
Create Date: 2021-03-24 17:32:15.012500

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "4c500cc3c90d"
down_revision = "8e1686ce62a0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "hardware",
        sa.Column(
            "deleted", oslo_db.sqlalchemy.types.SoftDeleteInteger(), nullable=True
        ),
    )
    op.add_column("hardware", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "worker_task",
        sa.Column(
            "deleted", oslo_db.sqlalchemy.types.SoftDeleteInteger(), nullable=True
        ),
    )
    op.add_column("worker_task", sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("worker_task", "deleted_at")
    op.drop_column("worker_task", "deleted")
    op.drop_column("hardware", "deleted_at")
    op.drop_column("hardware", "deleted")
