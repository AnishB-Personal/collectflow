"""add_format_fy_code_scope_to_export_tickets

Revision ID: c9a1e847f520
Revises: 77e73a1cb221
Create Date: 2026-11-02 05:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9a1e847f520'
down_revision: Union[str, Sequence[str], None] = '77e73a1cb221'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add format, fy_code, and scope columns to export_tickets."""
    op.add_column(
        'export_tickets',
        sa.Column('format', sa.String(length=20), nullable=True)
    )
    op.add_column(
        'export_tickets',
        sa.Column('fy_code', sa.String(length=20), nullable=True)
    )
    op.add_column(
        'export_tickets',
        sa.Column('scope', sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema to drop format, fy_code, and scope columns from export_tickets."""
    op.drop_column('export_tickets', 'scope')
    op.drop_column('export_tickets', 'fy_code')
    op.drop_column('export_tickets', 'format')
