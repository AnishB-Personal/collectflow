"""add_po_number_lower_trim_unique_index

Revision ID: e8b7c6d5e4f3
Revises: c9a1e847f520
Create Date: 2026-11-03 07:38:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e8b7c6d5e4f3'
down_revision: Union[str, Sequence[str], None] = 'c9a1e847f520'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Pre-migration assessment: verify no duplicate case-variant PO numbers exist
    if conn.dialect.name == "postgresql":
        dups = conn.execute(sa.text("""
            SELECT lower(trim(po_number)), count(*)
            FROM purchase_orders
            WHERE lower(trim(po_number)) NOT IN ('no po', 'recurring / direct invoice')
            GROUP BY lower(trim(po_number))
            HAVING count(*) > 1
        """)).fetchall()
        if dups:
            raise RuntimeError(f"Cannot apply migration: duplicate PO numbers found: {dups}")

        # Drop legacy case-sensitive unique constraint
        op.execute("ALTER TABLE purchase_orders DROP CONSTRAINT IF EXISTS purchase_orders_po_number_key CASCADE;")

        # Create concurrency-safe functional unique index with approved exceptions
        op.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_po_number_lower_trim
            ON purchase_orders (lower(trim(po_number)))
            WHERE lower(trim(po_number)) NOT IN ('no po', 'recurring / direct invoice');
        """)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_po_number_lower_trim;")
        op.create_unique_constraint('purchase_orders_po_number_key', 'purchase_orders', ['po_number'])
