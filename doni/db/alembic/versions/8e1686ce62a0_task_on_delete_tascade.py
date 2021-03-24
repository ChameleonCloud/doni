"""task_on_delete_tascade

Revision ID: 8e1686ce62a0
Revises: 555d45feb411
Create Date: 2021-03-24 16:56:51.983029

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8e1686ce62a0"
down_revision = "555d45feb411"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(None, "worker_task", type_="foreignkey")
    op.create_foreign_key(
        None, "worker_task", "hardware", ["hardware_uuid"], ["uuid"], ondelete="cascade"
    )


def downgrade():
    op.drop_constraint(None, "worker_task", type_="foreignkey")
    op.create_foreign_key(None, "worker_task", "hardware", ["hardware_uuid"], ["uuid"])
