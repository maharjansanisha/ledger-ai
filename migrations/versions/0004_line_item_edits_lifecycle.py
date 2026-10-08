"""line item edits lifecycle

Database-level guarantees for line_item_edits (see bahikhata/edit.py), so they hold
even if application code is wrong:

- A proposal is immutable: after INSERT only status and decided_at may change, so
  the target and values that Approve executes are exactly those that were shown.
- pending -> applied | cancelled | stale | expired, and those end states are final:
  a decided proposal can never be revived or decided again.
- decided_at is set exactly when the proposal is no longer pending.
- A proposal expires after it was created.

Plus a partial index for the expiry sweep (pending rows only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        ALTER TABLE line_item_edits
            ADD CONSTRAINT line_item_edits_decided_iff_final CHECK ((status = 'pending') = (decided_at IS NULL)),
            ADD CONSTRAINT line_item_edits_expires_after_created CHECK (expires_at > created_at)
        """
    )
    op.execute(
        """
        CREATE FUNCTION line_item_edits_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'pending' THEN
                RAISE EXCEPTION 'line_item_edits %: a % proposal is final', OLD.id, OLD.status;
            END IF;
            IF (NEW.id, NEW.session_id, NEW.receipt_id, NEW.line_item_id, NEW.line_no, NEW.field,
                NEW.row_snapshot, NEW.old_value, NEW.new_value, NEW.request_text, NEW.source,
                NEW.created_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.id, OLD.session_id, OLD.receipt_id, OLD.line_item_id, OLD.line_no, OLD.field,
                OLD.row_snapshot, OLD.old_value, OLD.new_value, OLD.request_text, OLD.source,
                OLD.created_at, OLD.expires_at) THEN
                RAISE EXCEPTION 'line_item_edits %: only status and decided_at may change', OLD.id;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER line_item_edits_immutable
            BEFORE UPDATE ON line_item_edits
            FOR EACH ROW EXECUTE FUNCTION line_item_edits_guard()
        """
    )
    op.execute("CREATE INDEX line_item_edits_pending_expiry ON line_item_edits (expires_at) WHERE status = 'pending'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS line_item_edits_pending_expiry")
    op.execute("DROP TRIGGER IF EXISTS line_item_edits_immutable ON line_item_edits")
    op.execute("DROP FUNCTION IF EXISTS line_item_edits_guard()")
    op.execute(
        """
        ALTER TABLE line_item_edits
            DROP CONSTRAINT IF EXISTS line_item_edits_expires_after_created,
            DROP CONSTRAINT IF EXISTS line_item_edits_decided_iff_final
        """
    )
