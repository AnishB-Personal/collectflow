"""add_must_change_password_to_users

Revision ID: 77e73a1cb221
Revises: e9ee322f8b96
Create Date: 2026-10-19 05:24:53.025372

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77e73a1cb221'
down_revision: Union[str, Sequence[str], None] = 'e9ee322f8b96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add must_change_password column."""
    op.add_column(
        'users',
        sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.text('false'))
    )


def downgrade() -> None:
    """Downgrade schema to drop must_change_password column."""
    op.drop_column('users', 'must_change_password')
