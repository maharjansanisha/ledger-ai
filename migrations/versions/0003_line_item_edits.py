"""line item edits

Proposals from the Ask Your Ledger chat to change ONE cell of ONE saved line
item, and what became of each (pending -> applied | cancelled | stale | expired).

The chat only ever INSERTs a 'pending' row here; the line item itself changes
only when the user clicks Approve, and then only through the proposal stored in
this table (see bahikhata/edit.py). Doubles as the per-change audit trail:
who asked (session_id), what they typed, the row as it was, old and new value,
and when it was decided.

line_item_id has no FK: re-saving a whole receipt from the Capture page replaces
its line items (new ids), and the history of a past chat edit must survive that.
Deliberately NO grant to ledger_reader (Ask Your Ledger cannot read it).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE line_item_edits (
            id            UUID        PRIMARY KEY,
            session_id    TEXT        NOT NULL,                 -- the chat session that may decide it
            receipt_id    BIGINT      NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
            line_item_id  BIGINT      NOT NULL,
            line_no       SMALLINT    NOT NULL,
            field         TEXT        NOT NULL CHECK (field IN ('description', 'quantity', 'unit_price', 'amount')),
            row_snapshot  JSONB       NOT NULL,                 -- the whole line as read when proposed
            old_value     JSONB       NOT NULL,                 -- JSON null when the cell was empty
            new_value     JSONB       NOT NULL,
            request_text  TEXT        NOT NULL,
            source        TEXT        NOT NULL DEFAULT 'chat_agent',
            status        TEXT        NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending', 'applied', 'cancelled', 'stale', 'expired')),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            decided_at    TIMESTAMPTZ
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS line_item_edits")
