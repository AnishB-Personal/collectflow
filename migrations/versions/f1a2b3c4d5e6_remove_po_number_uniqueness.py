"""remove_po_number_uniqueness

Revision ID: f1a2b3c4d5e6
Revises: e8b7c6d5e4f3
Create Date: 2026-11-03 08:21:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e8b7c6d5e4f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove PO number uniqueness to allow repeated external customer PO references."""
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        # Drop functional unique index
        op.execute("DROP INDEX IF EXISTS uq_po_number_lower_trim CASCADE;")
        # Drop any remaining legacy binary unique constraints/indexes
        op.execute("ALTER TABLE purchase_orders DROP CONSTRAINT IF EXISTS purchase_orders_po_number_key CASCADE;")
        op.execute("DROP INDEX IF EXISTS purchase_orders_po_number_key CASCADE;")
        # Create non-unique index to preserve fast customer-PO searching and correlation
        op.execute("CREATE INDEX IF NOT EXISTS idx_po_number ON purchase_orders (po_number);")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_po_number CASCADE;")
        op.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_po_number_lower_trim
            ON purchase_orders (lower(trim(po_number)))
            WHERE lower(trim(po_number)) NOT IN ('no po', 'recurring / direct invoice');
        """)
