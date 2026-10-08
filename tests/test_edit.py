"""Chat edits to saved line items (bahikhata/edit.py): propose in chat, apply only on Approve.

The first half needs no database: the LLM is a FakeClient and `connect` raises if
anything tries to reach the DB, which proves refusals happen before any read or write.
The second half runs the real SQL against TEST_DATABASE_URL (same safety rules as
tests/test_db.py: skipped if unset, refused unless the database name ends in "_test").
"""

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg.conninfo import conninfo_to_dict

from bahikhata import config, db, edit, sql_guard
from bahikhata.schemas import ConfirmedReceipt
from tests.fakes import FakeClient, server_error

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
SESSION = "session-a"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=1)
TODAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


def plan(status="edit", edits=(), clarification=None) -> str:
    return json.dumps({"status": status, "edits": [change(**e) for e in edits], "clarification": clarification})


def change(**fields) -> dict:
    return {"receipt_id": None, "merchant": None, "invoice_number": None, "item": None, "line_no": None,
            "field": "quantity", "current_value": None, "new_value": "5", **fields}


def no_db():
    raise AssertionError("the database must not be reached")


def propose_without_db(question, llm_reply):
    return edit.propose_edits(question, SESSION, now=NOW, client=FakeClient(llm_reply), connect=no_db)


# --- Request gate and pure checks -------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("Change the quantity of rice on receipt #1 to 5", True),
    ("please UPDATE oil price to 180", True),
    ("rename line 2 to Basmati rice", True),
    ("How much did I spend on rice last month?", False),
    ("Top 5 merchants by spend", False),
])
def test_looks_like_edit(question, expected):
    assert edit.looks_like_edit(question) is expected


REQUEST = "Change the quantity of rice on receipt #12 from 2 to 5"


@pytest.mark.parametrize("fields, outside", [
    ({"item": "rice", "receipt_id": 12, "current_value": "2", "new_value": "5"}, False),
    ({"item": "RICE", "new_value": "5"}, False),                          # case-insensitive
    ({"item": "wheat", "new_value": "5"}, True),                          # a line the user never named
    ({"item": "rice", "new_value": "6"}, True),                           # a value the user never typed
    ({"item": "rice", "receipt_id": 13, "new_value": "5"}, True),         # a different bill
    ({"item": "rice", "line_no": 3, "new_value": "5"}, True),             # a line number never given
    ({"item": "rice", "merchant": "Hari Stores", "new_value": "5"}, True),
    ({"item": "rice", "current_value": "3", "new_value": "5"}, True),
    ({"item": "rice", "new_value": "five"}, True),                        # no number at all
])
def test_outside_request_rejects_anything_the_user_did_not_write(fields, outside):
    assert edit.outside_request(edit._clean(edit.LineItemEditRequest(**change(**fields))), REQUEST) is outside


def test_outside_request_for_a_description_needs_the_new_text_in_the_request():
    request = "Rename line 2 on receipt #12 to Basmati rice"
    ok = edit.LineItemEditRequest(**change(line_no=2, receipt_id=12, field="description", new_value="basmati rice"))
    invented = edit.LineItemEditRequest(**change(line_no=2, receipt_id=12, field="description", new_value="Jasmine rice"))
    assert not edit.outside_request(ok, request)
    assert edit.outside_request(invented, request)


@pytest.mark.parametrize("field, text, expected", [
    ("quantity", "5", 5), ("quantity", "1.5 kg", __import__("decimal").Decimal("1.5")),
    ("quantity", "0", None), ("quantity", "-2", None), ("quantity", "lots", None),
    ("unit_price", "Rs 120", 12000), ("amount", "1,250.50", 125050), ("amount", "-5", None),
    ("description", "  Basmati   rice ", "Basmati rice"), ("description", "   ", None),
    ("description", "x" * (config.MAX_DESCRIPTION_CHARS + 1), None),
])
def test_parse_value_uses_the_review_form_parsers(field, text, expected):
    assert edit.parse_value(field, text) == expected


def test_line_items_edits_table_is_not_queryable_by_ask_your_ledger():
    assert not sql_guard.check_sql("SELECT * FROM line_item_edits").ok


def test_update_line_item_field_only_accepts_known_columns():
    with pytest.raises(KeyError):
        db.update_line_item_field(None, receipt_id=1, line_item_id=1, field="total_paisa = 0; --",
                                  new_value=0, snapshot={})


# --- Proposal phase: refusals happen before the database ---------------------------

def test_not_an_edit_falls_through_to_answering():
    assert propose_without_db("Change in spending last month?", plan("not_edit")) is None


def test_bulk_request_is_refused_not_guessed():
    result = propose_without_db("Change all the prices", plan("unsupported", clarification="Which item?"))
    assert result.edits == []
    assert "one at a time" in result.message


def test_ambiguous_request_asks_for_clarification():
    result = propose_without_db("Fix the rice line", plan("ambiguous", clarification="Which field, and to what?"))
    assert result.edits == [] and result.message == "Which field, and to what?"


def test_plan_that_broadens_the_scope_proposes_nothing():
    """The user named rice only; an over-eager plan also 'fixes' wheat. Nothing is proposed."""
    result = propose_without_db(
        "Change rice quantity on receipt #1 to 5",
        plan(edits=[{"receipt_id": 1, "item": "rice"}, {"receipt_id": 1, "item": "wheat"}]),
    )
    assert result.edits == []
    assert "haven't proposed anything" in result.message


def test_plan_that_changes_an_unrequested_field_proposes_nothing():
    """Quantity was asked; a plan that also sets the amount to a computed value is refused."""
    result = propose_without_db(
        "Change rice quantity on receipt #1 to 5",
        plan(edits=[{"receipt_id": 1, "item": "rice"},
                    {"receipt_id": 1, "item": "rice", "field": "amount", "new_value": "2500"}]),
    )
    assert result.edits == []


def test_edit_without_a_target_line_asks_which_item():
    result = propose_without_db("Set the quantity to 5", plan(edits=[{"new_value": "5"}]))
    assert result.edits == [] and "Which item" in result.message


def test_invalid_new_value_is_refused():
    result = propose_without_db("Change rice quantity to 0", plan(edits=[{"item": "rice", "new_value": "0"}]))
    assert result.edits == [] and "isn't a valid quantity" in result.message


def test_too_many_edits_are_refused():
    edits = [{"line_no": n, "new_value": "5"} for n in range(1, config.MAX_EDITS_PER_REQUEST + 2)]
    question = "Set quantity to 5 on lines " + " ".join(str(n) for n in range(1, config.MAX_EDITS_PER_REQUEST + 2))
    assert "at most" in propose_without_db(question, plan(edits=edits)).message


def test_llm_failure_is_a_message_not_an_exception(monkeypatch):
    monkeypatch.setattr(config, "LLM_RETRY_WAIT_SECONDS", 0)
    result = edit.propose_edits("Change rice quantity to 5", SESSION,
                                client=FakeClient(server_error(), server_error()), connect=no_db)
    assert result.edits == [] and result.message


def test_unparseable_plan_is_a_message():
    result = propose_without_db("Change rice quantity to 5", "not json")
    assert result.edits == [] and "rephrasing" in result.message


def test_approve_with_a_malformed_id_never_reaches_the_database():
    assert edit.apply_edit("../../etc", SESSION, connect=no_db).status == "unavailable"
    assert edit.cancel_edit("", SESSION, connect=no_db).status == "unavailable"


# --- Database tests --------------------------------------------------------------

requires_test_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set: create Neon 'ledger_test', add it to .env, run `make migrate TARGET=test`",
)


def connect():
    return db.get_connection(TEST_DATABASE_URL)


@pytest.fixture
def conn():
    if TEST_DATABASE_URL == os.getenv("DATABASE_URL", "").strip():
        pytest.fail("Refusing: TEST_DATABASE_URL equals DATABASE_URL (the real ledger).")
    dbname = conninfo_to_dict(TEST_DATABASE_URL).get("dbname") or ""
    if not dbname.endswith("_test"):
        pytest.fail(f"Refusing: test database name must end in '_test' (got {dbname!r}).")
    with connect() as connection:
        connection.autocommit = True  # see other connections' commits immediately
        connection.execute("TRUNCATE receipts, line_items, receipt_audit, line_item_edits RESTART IDENTITY CASCADE")
        yield connection


def _line(description, quantity, unit_price_paisa):
    return {"description": description, "quantity": str(quantity),
            "unit_price_paisa": unit_price_paisa, "amount_paisa": quantity * unit_price_paisa}


def save(conn, merchant, invoice, lines) -> int:
    receipt = ConfirmedReceipt.model_validate({
        "merchant_name": merchant, "invoice_number": invoice, "date_ad": "2026-09-30", "date_bs": "2083-06-14",
        "bs_year": 2083, "bs_month": 6, "total_paisa": sum(line["amount_paisa"] for line in lines),
        "category": "Inventory", "status": "clean", "line_items": lines,
    })
    audit = db.AuditRecord(
        model_name="m", prompt_version="extraction_v1", image_sha256="ab" * 32,
        ai_draft_json={"merchant_name": merchant}, flags_at_extraction_json=[], flags_at_save_json=[],
        edited_fields_json=[], confirmed_at=NOW,
    )
    return db.save_confirmed_receipt(conn, receipt, "data/images/x.jpg", audit)


@pytest.fixture
def ledger(conn):
    """Receipt #1: Rice ×2, Rice ×3, Wheat ×4 (the 'similar rows' case). Receipt #2: Rice ×2."""
    first = save(conn, "Shree Traders", "INV-1",
                 [_line("Rice", 2, 50000), _line("Rice", 3, 50000), _line("Wheat", 4, 25000)])
    second = save(conn, "Hari Stores", "INV-2", [_line("Rice", 2, 50000)])
    return first, second


def tables(conn) -> dict:
    """Every row of every bill table, to compare before and after."""
    out = {}
    with conn.cursor() as cur:
        for table, order in (("receipts", "id"), ("line_items", "id"), ("receipt_audit", "receipt_id")):
            cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
            out[table] = cur.fetchall()
    return out


def quantities(conn) -> list[tuple]:
    return conn.execute("SELECT receipt_id, line_no, description, quantity FROM line_items ORDER BY id").fetchall()


def proposal_statuses(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM line_item_edits ORDER BY created_at, id").fetchall()]


def propose(question, edits, *, status="edit", session=SESSION):
    return edit.propose_edits(question, session, now=NOW, client=FakeClient(plan(status, edits)), connect=connect)


def approve(result, index=0, *, session=SESSION, now=LATER):
    return edit.apply_edit(result.edits[index].proposal_id, session, now=now, today=TODAY, connect=connect)


RICE_2_TO_5 = ("Change Rice quantity on receipt #1 from 2 to 5",
               [{"receipt_id": 1, "item": "Rice", "current_value": "2", "new_value": "5"}])


@requires_test_db
def test_request_creates_a_proposal_and_changes_no_bill(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    assert result.message is None and len(result.edits) == 1
    proposal = result.edits[0]
    assert (proposal.receipt_id, proposal.line_no, proposal.item, proposal.field) == (1, 1, "Rice", "quantity")
    assert (proposal.current_text, proposal.new_text) == ("2", "5")
    assert (proposal.merchant_name, proposal.invoice_number) == ("Shree Traders", "INV-1")
    assert tables(conn) == before
    assert proposal_statuses(conn) == ["pending"]


@requires_test_db
def test_approve_changes_only_the_requested_cell(ledger, conn):
    """Row A: Rice 2 -> 5. Row B (also Rice), Row C (Wheat) and receipt #2's Rice stay as they were."""
    before = tables(conn)
    outcome = approve(propose(*RICE_2_TO_5))
    assert outcome.applied
    assert "line 1 (Rice): quantity 2 → 5" in outcome.message
    assert quantities(conn) == [(1, 1, "Rice", 5.0), (1, 2, "Rice", 3.0), (1, 3, "Wheat", 4.0), (2, 1, "Rice", 2.0)]

    after = tables(conn)
    changed_line, = [(b, a) for b, a in zip(before["line_items"], after["line_items"]) if b != a]
    assert changed_line[0][:4] == changed_line[1][:4]  # id, receipt_id, line_no, description
    assert changed_line[0][5:] == changed_line[1][5:]  # unit price and amount untouched (not "derived")
    assert after["receipts"][1] == before["receipts"][1]          # the other bill
    assert after["receipt_audit"][1] == before["receipt_audit"][1]
    status_col = 14  # receipts.status
    first_before, first_after = before["receipts"][0], after["receipts"][0]
    assert first_after[:status_col] == first_before[:status_col]
    assert first_after[status_col + 1:] == first_before[status_col + 1:]
    assert proposal_statuses(conn) == ["applied"]


@requires_test_db
def test_derived_status_follows_the_validator(ledger, conn):
    """5 × Rs 500 ≠ Rs 1,000 is a V3 warning: the receipt is re-derived as needs_review, and the
    amount is NOT silently recomputed (the validator detects, never corrects)."""
    outcome = approve(propose(*RICE_2_TO_5))
    assert "needs review" in outcome.message
    status, = conn.execute("SELECT status FROM receipts WHERE id = 1").fetchone()
    flags, edited = conn.execute(
        "SELECT flags_at_save_json, edited_fields_json FROM receipt_audit WHERE receipt_id = 1").fetchone()
    assert status == "needs_review"
    assert [f["rule_id"] for f in flags] == ["V3"]
    assert "line_items" in edited


@requires_test_db
def test_cancel_changes_nothing_and_the_proposal_cannot_be_approved_later(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    cancelled = edit.cancel_edit(result.edits[0].proposal_id, SESSION, now=LATER, connect=connect)
    assert cancelled.status == "cancelled"
    assert approve(result).status == "already_decided"
    assert tables(conn) == before
    assert proposal_statuses(conn) == ["cancelled"]


@requires_test_db
def test_approving_twice_applies_once(ledger, conn):
    result = propose(*RICE_2_TO_5)
    assert approve(result).applied
    conn.execute("UPDATE line_items SET quantity = 2 WHERE receipt_id = 1 AND line_no = 1")  # someone reverts it
    again = approve(result)
    assert again.status == "already_decided" and not again.applied
    assert quantities(conn)[0] == (1, 1, "Rice", 2.0)  # the replay did not re-apply 5


@requires_test_db
def test_concurrent_approvals_apply_once(ledger, conn):
    result = propose(*RICE_2_TO_5)
    barrier, outcomes = threading.Barrier(2), []

    def click():
        barrier.wait()
        outcomes.append(approve(result).status)

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["already_decided", "applied"]
    assert quantities(conn)[0] == (1, 1, "Rice", 5.0)


@requires_test_db
def test_stale_proposal_is_rejected_and_shows_the_latest_value(ledger, conn):
    result = propose(*RICE_2_TO_5)
    conn.execute("UPDATE line_items SET quantity = 4 WHERE receipt_id = 1 AND line_no = 1")  # another change
    outcome = approve(result)
    assert outcome.status == "stale" and not outcome.applied
    assert "quantity 4" in outcome.message
    assert quantities(conn)[0] == (1, 1, "Rice", 4.0)  # the newer value is not overwritten
    assert proposal_statuses(conn) == ["stale"]


@requires_test_db
def test_a_change_elsewhere_on_the_line_also_makes_the_proposal_stale(ledger, conn):
    result = propose(*RICE_2_TO_5)
    conn.execute("UPDATE line_items SET description = 'Basmati Rice' WHERE receipt_id = 1 AND line_no = 1")
    assert approve(result).status == "stale"
    assert quantities(conn)[0] == (1, 1, "Basmati Rice", 2.0)


@requires_test_db
def test_resaving_the_whole_receipt_makes_the_proposal_stale(ledger, conn):
    result = propose(*RICE_2_TO_5)
    receipt = ConfirmedReceipt.model_validate({
        "merchant_name": "Shree Traders", "date_ad": "2026-09-30", "date_bs": "2083-06-14", "bs_year": 2083,
        "bs_month": 6, "total_paisa": 100000, "category": "Inventory", "status": "clean",
        "line_items": [_line("Rice", 2, 50000)],
    })
    db.update_confirmed_receipt(conn, 1, receipt, flags_at_save=[], edited_fields=[], confirmed_at=NOW)
    outcome = approve(result)
    assert outcome.status == "stale" and "no longer exists" in outcome.message
    assert [row for row in quantities(conn) if row[0] == 1] == [(1, 1, "Rice", 2.0)]  # re-inserted line, new id


@requires_test_db
def test_expired_proposal_is_rejected(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    outcome = approve(result, now=NOW + timedelta(minutes=config.EDIT_PROPOSAL_TTL_MINUTES, seconds=1))
    assert outcome.status == "expired"
    assert tables(conn) == before
    assert proposal_statuses(conn) == ["expired"]


@requires_test_db
def test_another_session_cannot_approve_or_cancel(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    assert approve(result, session="session-b").status == "unavailable"
    assert edit.cancel_edit(result.edits[0].proposal_id, "session-b", connect=connect).status == "unavailable"
    assert tables(conn) == before
    assert approve(result).applied  # still pending for its own session


@requires_test_db
def test_tampered_card_values_cannot_redirect_the_change(ledger, conn):
    """Approve sends back the proposal id only; whatever the client holds about the target is
    ignored, and the stored proposal is applied exactly as proposed."""
    result = propose(*RICE_2_TO_5)
    result.edits[0].receipt_id, result.edits[0].line_no, result.edits[0].new_text = 2, 1, "999"
    assert approve(result).applied
    assert quantities(conn) == [(1, 1, "Rice", 5.0), (1, 2, "Rice", 3.0), (1, 3, "Wheat", 4.0), (2, 1, "Rice", 2.0)]


@requires_test_db
def test_unknown_proposal_id_is_rejected(ledger, conn):
    assert edit.apply_edit("00000000-0000-0000-0000-000000000000", SESSION, connect=connect).status == "unavailable"


@requires_test_db
def test_several_matching_rows_ask_which_one(ledger, conn):
    """Scenario F: "Change rice price to 100" matches three Rice lines -> clarify, propose nothing."""
    result = propose("Change rice price to 100", [{"item": "rice", "field": "unit_price", "new_value": "100"}])
    assert result.edits == []
    assert "more than one line" in result.message
    assert "receipt #1 (Shree Traders) line 1" in result.message and "receipt #2 (Hari Stores) line 1" in result.message
    assert proposal_statuses(conn) == []


@requires_test_db
def test_current_value_alone_does_not_pick_between_bills(ledger, conn):
    """Rice ×2 is on both receipts: without a bill, still ambiguous."""
    result = propose("Change Rice quantity from 2 to 5",
                     [{"item": "Rice", "current_value": "2", "new_value": "5"}])
    assert result.edits == [] and "more than one line" in result.message


@requires_test_db
def test_current_value_that_does_not_match_shows_what_is_there(ledger, conn):
    result = propose("Change wheat quantity on receipt #1 from 7 to 5",
                     [{"receipt_id": 1, "item": "wheat", "current_value": "7", "new_value": "5"}])
    assert result.edits == []
    assert "quantity 7" in result.message and "Wheat, quantity 4" in result.message


@requires_test_db
def test_unknown_item_is_not_found(ledger, conn):
    result = propose("Change sugar quantity on receipt #1 to 2", [{"receipt_id": 1, "item": "sugar", "new_value": "2"}])
    assert result.edits == [] and "couldn't find “sugar” on receipt #1" in result.message


@requires_test_db
def test_unchanged_value_proposes_nothing(ledger, conn):
    result = propose("Set wheat quantity on receipt #1 to 4", [{"receipt_id": 1, "item": "wheat", "new_value": "4"}])
    assert result.edits == [] and "already 4" in result.message


@requires_test_db
def test_several_explicitly_named_rows_become_separate_proposals(ledger, conn):
    result = propose(
        "On receipt #1 change wheat quantity to 6 and the amount on line 2 to Rs 1,600",
        [{"receipt_id": 1, "item": "wheat", "new_value": "6"},
         {"receipt_id": 1, "line_no": 2, "field": "amount", "new_value": "Rs 1,600"}],
    )
    assert [(p.line_no, p.field, p.new_text) for p in result.edits] == [(3, "quantity", "6"), (2, "amount", "Rs 1,600.00")]
    assert all(approve(result, i).applied for i in range(2))
    rows = conn.execute("SELECT receipt_id, line_no, quantity, amount_paisa FROM line_items ORDER BY id").fetchall()
    assert rows == [(1, 1, 2.0, 100000), (1, 2, 3.0, 160000), (1, 3, 6.0, 100000), (2, 1, 2.0, 100000)]


@requires_test_db
def test_one_unresolvable_edit_means_none_are_proposed(ledger, conn):
    result = propose(
        "On receipt #1 change wheat quantity to 6 and sugar quantity to 2",
        [{"receipt_id": 1, "item": "wheat", "new_value": "6"}, {"receipt_id": 1, "item": "sugar", "new_value": "2"}],
    )
    assert result.edits == [] and proposal_statuses(conn) == []


@requires_test_db
def test_description_edit(ledger, conn):
    result = propose("Rename line 3 on receipt #1 to Atta flour",
                     [{"receipt_id": 1, "line_no": 3, "field": "description", "new_value": "Atta flour"}])
    assert (result.edits[0].current_text, result.edits[0].new_text) == ("“Wheat”", "“Atta flour”")
    assert approve(result).applied
    assert quantities(conn)[2] == (1, 3, "Atta flour", 4.0)


@requires_test_db
def test_merchant_and_like_wildcards_are_matched_literally(ledger, conn):
    result = propose("Change %ice quantity on the Hari Stores bill to 5",
                     [{"merchant": "Hari Stores", "item": "%ice", "new_value": "5"}])
    assert result.edits == [] and "couldn't find" in result.message


@requires_test_db
def test_update_line_item_field_needs_the_exact_receipt_and_line(ledger, conn):
    snapshot = {"line_no": 1, "description": "Rice", "quantity": 2.0, "unit_price_paisa": 50000, "amount_paisa": 100000}
    line_id, = conn.execute("SELECT id FROM line_items WHERE receipt_id = 1 AND line_no = 1").fetchone()
    with conn.transaction():
        assert not db.update_line_item_field(conn, receipt_id=2, line_item_id=line_id, field="quantity",
                                             new_value=9.0, snapshot=snapshot)
    assert all(q != 9.0 for *_, q in quantities(conn))
