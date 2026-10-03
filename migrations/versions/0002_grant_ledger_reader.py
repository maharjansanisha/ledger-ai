"""grant ledger reader

Read-only access for "Ask Your Ledger" (Amendment A1, TASK-005 / TASK-009).
The ledger_reader role is created by hand in the Neon SQL editor (it needs a
password, which must never be committed):

    CREATE ROLE ledger_reader WITH LOGIN PASSWORD '<choose-a-strong-password>';

This migration only grants it the minimum: SELECT on the two queryable
tables. Deliberately NO grant on receipt_audit or alembic_version.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03 14:09:35.704701

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_reader') THEN
                RAISE EXCEPTION 'Role ledger_reader does not exist. Create it first in the Neon SQL editor: CREATE ROLE ledger_reader WITH LOGIN PASSWORD ''...'';';
            END IF;
        END
        $$
        """
    )

    # Start from nothing, then grant only what is needed.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ledger_reader")
    op.execute("GRANT USAGE ON SCHEMA public TO ledger_reader")
    op.execute("GRANT SELECT ON receipts, line_items TO ledger_reader")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_reader') THEN
                REVOKE SELECT ON receipts, line_items FROM ledger_reader;
                REVOKE USAGE ON SCHEMA public FROM ledger_reader;
            END IF;
        END
        $$
        """
    )
