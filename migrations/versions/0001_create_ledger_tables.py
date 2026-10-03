"""create ledger tables

Ledger tables (ARCHITECTURE.md §7 with Amendment A1 column types).
Money is integer PAISA in BIGINT columns ending in _paisa (decision D1).
receipts + line_items = human-confirmed ledger (queryable by "Ask Your Ledger").
receipt_audit = what the AI said (NOT queryable; no grant to ledger_reader --
see 0002_grant_ledger_reader).

Revision ID: 0001
Revises:
Create Date: 2026-10-03 14:09:35.519984

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE receipts (
            id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            merchant_name         TEXT        NOT NULL CHECK (length(btrim(merchant_name)) > 0),
            merchant_pan          TEXT,
            invoice_number        TEXT,
            date_ad               DATE        NOT NULL,
            date_bs               TEXT        NOT NULL,                       -- 'YYYY-MM-DD' in BS
            bs_year               SMALLINT    NOT NULL,
            bs_month              SMALLINT    NOT NULL CHECK (bs_month BETWEEN 1 AND 12),  -- 1 = Baisakh
            subtotal_paisa        BIGINT      CHECK (subtotal_paisa >= 0),    -- NULL = not printed
            discount_paisa        BIGINT      CHECK (discount_paisa >= 0),
            service_charge_paisa  BIGINT      CHECK (service_charge_paisa >= 0),
            vat_paisa             BIGINT      CHECK (vat_paisa >= 0),         -- NULL for VAT-inclusive bills
            total_paisa           BIGINT      NOT NULL CHECK (total_paisa > 0),
            category              TEXT        NOT NULL CHECK (category IN (
                                      'Inventory', 'Office Supplies', 'Transportation', 'Utilities',
                                      'Rent', 'Equipment', 'Marketing', 'Food', 'Other')),
            status                TEXT        NOT NULL CHECK (status IN ('clean', 'needs_review', 'invalid')),
            user_override         BOOLEAN     NOT NULL DEFAULT FALSE,
            image_path            TEXT        NOT NULL,                       -- relative path under data/images/
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE line_items (
            id                BIGINT   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            receipt_id        BIGINT   NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
            line_no           SMALLINT NOT NULL CHECK (line_no >= 1),        -- order on the receipt
            description       TEXT,
            quantity          REAL,                                          -- not money (e.g. 1.5 kg)
            unit_price_paisa  BIGINT   CHECK (unit_price_paisa >= 0),
            amount_paisa      BIGINT   CHECK (amount_paisa >= 0),
            UNIQUE (receipt_id, line_no)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE receipt_audit (
            receipt_id                BIGINT      PRIMARY KEY REFERENCES receipts (id) ON DELETE CASCADE,
            model_name                TEXT        NOT NULL,
            prompt_version            TEXT        NOT NULL,
            image_sha256              TEXT        NOT NULL,
            raw_response              TEXT,                    -- raw model text, unmodified
            ai_extraction_json        JSONB,                   -- parsed ReceiptExtraction (AI values)
            ai_draft_json             JSONB,                   -- normalized ReceiptDraft before human edits
            flags_at_extraction_json  JSONB       NOT NULL,
            flags_at_save_json        JSONB       NOT NULL,
            edited_fields_json        JSONB       NOT NULL,    -- e.g. ["total_paisa", "line_items"]
            extracted_at              TIMESTAMPTZ,
            confirmed_at              TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Reverse FK dependency order: receipt_audit/line_items reference receipts.
    op.execute("DROP TABLE IF EXISTS receipt_audit")
    op.execute("DROP TABLE IF EXISTS line_items")
    op.execute("DROP TABLE IF EXISTS receipts")
