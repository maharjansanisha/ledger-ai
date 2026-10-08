"""Chat edits to saved line items (bahikhata/edit.py): propose in chat, apply only on Approve.

The first half needs no database: the LLM is a FakeClient and `connect` raises if
anything tries to reach the DB, which proves refusals happen before any read or write.
The second half runs the real SQL against TEST_DATABASE_URL (same safety rules as
tests/test_db.py: skipped if unset, refused unless the database name ends in "_test").
"""

import inspect
import json
import os
import uuid
import threading
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from bahikhata import config, db, edit, llm_client, sql_guard
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
    invented = edit.LineItemEditRequest(**change(line_no=2, receipt_id=12, field="description",
                                                 new_value="Jasmine rice"))
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
    card = result.edits[0]
    card.receipt_id, card.line_no, card.field, card.current_text, card.new_text = 2, 3, "amount", "4", "999"
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
    assert [(p.line_no, p.field, p.new_text) for p in result.edits] == [
        (3, "quantity", "6"), (2, "amount", "Rs 1,600.00")]
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



# --- Receipt grounding: the server picks the bill, never the LLM ---------------------

@pytest.mark.parametrize("request_text, expected", [
    ("Change rice on receipt #12 to 5", {12}),
    ("change rice on Receipt 12 to 5", {12}),
    ("bill no. 7, line 2: set quantity to 3", {7}),
    ("on #5 set line 2 to 3", {5}),
    ("set line #2 on receipt 4 to 3", {4}),
    ("Change the rice price to 120", set()),          # a number that isn't a receipt number
    ("receipt 100 earlier; now receipt 200 rice to 100", {100, 200}),
])
def test_receipt_refs_only_counts_numbers_written_as_receipt_numbers(request_text, expected):
    assert edit.receipt_refs(request_text) == expected


def test_line_refs():
    assert edit.line_refs("set line #2 and row 3 on receipt 4 to 5") == {2, 3}


def req(**fields):
    return edit.LineItemEditRequest(**change(**fields))


@pytest.mark.parametrize("request_text, edits, active, expected_receipt, problem", [
    ("Change rice on receipt #2 to 5", [req(item="rice", receipt_id=2)], None, 2, None),
    ("Change rice to 5", [req(item="rice")], None, None, None),                       # no bill named
    ("Change rice to 5", [req(item="rice", receipt_id=2)], None, None, "haven't proposed"),  # invented
    ("Change rice on receipt #1 to 2", [req(item="rice", receipt_id=2)], None, None, "haven't proposed"),
    ("Seen receipt 1; change rice on receipt 2 to 5", [req(item="rice", receipt_id=2)], None, None, "more than one"),
    ("Change rice on receipt #2 to 5", [req(item="rice")], None, None, "wasn't sure"),  # model dropped it
    ("Change rice to 5", [req(item="rice")], 2, 2, None),                              # trusted context
    ("Change rice on receipt #2 to 5", [req(item="rice", receipt_id=2)], 2, 2, None),
    ("Change rice on receipt #1 to 5", [req(item="rice", receipt_id=1)], 2, None, "working on receipt #2"),
    ("Change rice to 5", [req(item="rice", receipt_id=1)], 2, None, "haven't proposed"),
])
def test_ground_receipt(request_text, edits, active, expected_receipt, problem):
    receipt_id, message = edit.ground_receipt(edits, request_text, active)
    assert receipt_id == expected_receipt
    assert (message is None) if problem is None else (problem in message)


@requires_test_db
def test_grounding_a_invented_receipt_proposes_nothing(ledger, conn):
    before = tables(conn)
    result = propose("Change wheat quantity to 6", [{"receipt_id": 2, "item": "wheat", "new_value": "6"}])
    assert result.edits == [] and proposal_statuses(conn) == [] and tables(conn) == before


@requires_test_db
def test_grounding_f_receipt_number_that_only_coincides_with_another_number(ledger, conn):
    """'1' is in the message, but as a quantity: the model can't use it to pick receipt #1."""
    result = propose("Change wheat quantity to 1", [{"receipt_id": 1, "item": "wheat", "new_value": "1"}])
    assert result.edits == [] and proposal_statuses(conn) == []


@requires_test_db
@pytest.mark.parametrize("llm_receipt", [1, 2])
def test_grounding_b_receipt_mentioned_in_passing_is_never_picked(ledger, conn, llm_receipt):
    question = "I was looking at receipt 1 while discussing another bill. Change the rice price on receipt 2 to 100."
    result = propose(question, [{"receipt_id": llm_receipt, "item": "rice", "field": "unit_price", "new_value": "100"}])
    assert result.edits == [] and "more than one receipt (#1, #2)" in result.message
    assert proposal_statuses(conn) == []


@requires_test_db
def test_grounding_named_receipt_the_model_did_not_target_is_a_question(ledger, conn):
    result = propose("Change rice on receipt #2 quantity to 5", [{"item": "rice", "new_value": "5"}])
    assert result.edits == [] and "wasn't sure receipt #2" in result.message


@requires_test_db
def test_grounding_c_same_merchant_on_two_receipts_asks_which(ledger, conn):
    save(conn, "Shree Traders", "INV-3", [_line("Rice", 2, 50000)])
    result = propose("Change rice quantity on the Shree Traders bill from 2 to 5",
                     [{"merchant": "Shree Traders", "item": "rice", "current_value": "2", "new_value": "5"}])
    assert result.edits == [] and "more than one line" in result.message
    assert "receipt #1" in result.message and "receipt #3" in result.message
    assert proposal_statuses(conn) == []


@requires_test_db
def test_grounding_d_trusted_active_receipt_resolves_the_line(ledger, conn):
    """Without context "Rice" is on several bills; with receipt #2 as trusted context it is one line."""
    result = edit.propose_edits("Change Rice quantity to 5", SESSION, active_receipt_id=2, now=NOW,
                                client=FakeClient(plan(edits=[{"item": "Rice", "new_value": "5"}])), connect=connect)
    assert [(p.receipt_id, p.line_no) for p in result.edits] == [(2, 1)]
    stored, = conn.execute("SELECT receipt_id, line_item_id FROM line_item_edits").fetchall()
    assert stored == (2, conn.execute("SELECT id FROM line_items WHERE receipt_id = 2").fetchone()[0])
    assert approve(result).applied
    assert quantities(conn) == [(1, 1, "Rice", 2.0), (1, 2, "Rice", 3.0), (1, 3, "Wheat", 4.0), (2, 1, "Rice", 5.0)]


@requires_test_db
def test_grounding_d_model_cannot_redirect_away_from_the_active_receipt(ledger, conn):
    result = edit.propose_edits("Change Rice quantity to 5", SESSION, active_receipt_id=2, now=NOW,
                                client=FakeClient(plan(edits=[{"receipt_id": 1, "item": "Rice", "new_value": "5"}])),
                                connect=connect)
    assert result.edits == [] and proposal_statuses(conn) == []


@requires_test_db
def test_line_number_that_only_coincides_with_a_value_is_rejected(ledger, conn):
    result = propose("Set wheat quantity on receipt #1 to 2",
                     [{"receipt_id": 1, "item": "wheat", "line_no": 2, "new_value": "2"}])
    assert result.edits == [] and proposal_statuses(conn) == []


# --- Expiry and cleanup ---------------------------------------------------------------

TTL = timedelta(minutes=config.EDIT_PROPOSAL_TTL_MINUTES)


def proposal_row(conn) -> dict:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute("SELECT * FROM line_item_edits").fetchone()


@requires_test_db
def test_expiry_a_proposal_within_its_ttl_stays_pending(ledger, conn):
    propose(*RICE_2_TO_5)
    assert db.expire_edit_proposals(conn, NOW + TTL - timedelta(seconds=1)) == 0
    assert proposal_statuses(conn) == ["pending"]


@requires_test_db
def test_expiry_b_and_d_crossing_the_ttl_expires_it_and_keeps_the_record(ledger, conn):
    propose(*RICE_2_TO_5)
    before = proposal_row(conn)
    assert db.expire_edit_proposals(conn, NOW + TTL) == 1
    after = proposal_row(conn)
    assert (after["status"], after["decided_at"]) == ("expired", before["expires_at"])
    unchanged = {k: v for k, v in after.items() if k not in ("status", "decided_at")}
    assert unchanged == {k: v for k, v in before.items() if k not in ("status", "decided_at")}
    assert (after["request_text"], after["session_id"], after["source"]) == (RICE_2_TO_5[0], SESSION, "chat_agent")
    assert (after["old_value"], after["new_value"], after["field"], after["receipt_id"]) == ("2", "5", "quantity", 1)


@requires_test_db
def test_expiry_c_and_e_expired_proposal_cannot_be_applied_and_cleanup_is_idempotent(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    db.expire_edit_proposals(conn, NOW + TTL)
    expired_row = proposal_row(conn)
    assert db.expire_edit_proposals(conn, NOW + 2 * TTL) == 0
    assert proposal_row(conn) == expired_row
    assert approve(result, now=NOW + TTL).status == "expired"
    assert tables(conn) == before and proposal_row(conn) == expired_row


@requires_test_db
def test_expiry_approval_enforces_it_even_if_cleanup_never_ran(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    assert proposal_statuses(conn) == ["pending"]
    assert approve(result, now=NOW + TTL).status == "expired"  # exactly at expires_at: already too late
    assert tables(conn) == before and proposal_statuses(conn) == ["expired"]


@requires_test_db
def test_expiry_f_tampering_cannot_revive_an_expired_proposal(ledger, conn):
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    db.expire_edit_proposals(conn, NOW + TTL)
    result.edits[0].new_text = "5"
    assert approve(result, now=NOW).status == "expired"  # even a client clock "before expiry" can't help
    with pytest.raises(psycopg.errors.RaiseException, match="final"):
        conn.execute("UPDATE line_item_edits SET status = 'pending', decided_at = NULL")
    assert tables(conn) == before and proposal_statuses(conn) == ["expired"]


@requires_test_db
def test_proposing_sweeps_expired_proposals(ledger, conn):
    propose(*RICE_2_TO_5)
    edit.propose_edits("Set wheat quantity on receipt #1 to 6", SESSION, now=NOW + TTL, connect=connect,
                       client=FakeClient(plan(edits=[{"receipt_id": 1, "item": "wheat", "new_value": "6"}])))
    assert sorted(proposal_statuses(conn)) == ["expired", "pending"]


@requires_test_db
def test_proposing_sweeps_expired_proposals_even_when_nothing_is_proposed(ledger, conn):
    propose(*RICE_2_TO_5)
    result = edit.propose_edits("Change sugar quantity on receipt #1 to 2", SESSION, now=NOW + TTL, connect=connect,
                                client=FakeClient(plan(edits=[{"receipt_id": 1, "item": "sugar", "new_value": "2"}])))
    assert result.edits == [] and proposal_statuses(conn) == ["expired"]


# --- Database-level invariants on line_item_edits --------------------------------------

@requires_test_db
def test_a_pending_proposal_target_and_values_are_immutable(ledger, conn):
    result = propose(*RICE_2_TO_5)
    for assignment in ("new_value = '\"9\"'::jsonb", "line_item_id = line_item_id + 1", "receipt_id = 2",
                       "field = 'amount'", "expires_at = expires_at + interval '1 day'"):
        with pytest.raises(psycopg.errors.RaiseException, match="only status and decided_at"):
            conn.execute(f"UPDATE line_item_edits SET {assignment}")
    assert approve(result).applied
    assert quantities(conn)[0] == (1, 1, "Rice", 5.0)


@requires_test_db
def test_a_decided_proposal_is_final_in_the_database(ledger, conn):
    approve(propose(*RICE_2_TO_5))
    with pytest.raises(psycopg.errors.RaiseException, match="applied proposal is final"):
        conn.execute("UPDATE line_item_edits SET status = 'cancelled'")


@requires_test_db
def test_decided_at_is_set_exactly_when_decided(ledger, conn):
    propose(*RICE_2_TO_5)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE line_item_edits SET status = 'cancelled'")  # no decided_at


# --- Affected-row invariant -------------------------------------------------------------

def test_update_line_item_field_treats_more_than_one_row_as_a_failure():
    class Cursor:
        rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args):
            pass

    snapshot = dict.fromkeys(db.SNAPSHOT_COLUMNS)
    with pytest.raises(RuntimeError, match="matched 2 rows"):
        db.update_line_item_field(SimpleNamespace(cursor=Cursor), receipt_id=1, line_item_id=1, field="quantity",
                                  new_value=5.0, snapshot=snapshot)


@requires_test_db
def test_a_failed_invariant_rolls_back_and_is_not_reported_as_applied(ledger, conn, monkeypatch):
    """Even if the write had happened before the invariant check failed, it is rolled back."""
    before = tables(conn)
    result = propose(*RICE_2_TO_5)
    real_update = db.update_line_item_field

    def update_then_fail(*args, **kwargs):
        real_update(*args, **kwargs)
        raise RuntimeError("line item update matched 2 rows; expected at most 1")

    monkeypatch.setattr(db, "update_line_item_field", update_then_fail)
    outcome = approve(result)
    assert outcome.status == "failed" and not outcome.applied
    assert tables(conn) == before and proposal_statuses(conn) == ["pending"]


# --- Deterministic refusals: delete and bill-level requests never become proposals -------

@pytest.mark.parametrize("request_text", [
    "delete entry for ambikeshar pharma",
    "Remove the rice line from receipt #1",
    "update the total paisa of ambikeshar pharma to Rs 4000",
    "change the merchant name of ambikehsar ayurvedic pharma to ambik pharma",
    "Set the VAT on receipt #1 to 130",
    "Change the date of receipt #1 to 2026-09-01",
    "Change the category of receipt #1 to Food",
])
def test_refusal_for_delete_and_bill_level_requests(request_text):
    assert edit.refusal_for(request_text) is not None


@pytest.mark.parametrize("request_text", [
    "Change the description of Rice to Basmati Rice",
    "Change Rice quantity to 5",
    "Change Rice unit price to Rs 120",
    "Change Rice amount to Rs 600",
    "Rename line 1 on receipt #1 to Ambik Pharma",   # a description edit, not a merchant edit
    "Change rice quantity to 5 for Ambikeshar Pharma",
])
def test_line_item_requests_are_not_refused(request_text):
    assert edit.refusal_for(request_text) is None


def test_delete_requests_reach_the_planner_so_they_get_a_clear_refusal():
    assert edit.looks_like_edit("delete entry for ambikeshar pharma")


@pytest.mark.parametrize("request_text, llm_edit, expected", [
    # The model wrongly turns a delete / a total / a merchant rename into a line-item edit: refused anyway.
    ("delete entry for ambikeshar pharma",
     {"merchant": "ambikeshar pharma", "item": "entry", "field": "description", "new_value": "entry"}, "Deleting"),
    ("update the total paisa of ambikeshar pharma to Rs 4000",
     {"merchant": "ambikeshar pharma", "item": "total", "field": "amount", "new_value": "Rs 4000"}, "Bill-level"),
    ("change the merchant name of ambikehsar ayurvedic pharma to ambik pharma",
     {"item": "ambikehsar ayurvedic pharma", "field": "description", "new_value": "ambik pharma"},
     "I can’t change the merchant/shop name"),
])
@pytest.mark.parametrize("status", ["edit", "unsupported", "ambiguous"])
def test_misclassified_delete_or_bill_level_edit_is_refused_before_the_database(
    request_text, llm_edit, expected, status,
):
    edits = [llm_edit] if status == "edit" else []
    result = propose_without_db(request_text, plan(status, edits))
    assert result.edits == [] and result.message.startswith(expected)


def test_a_question_that_mentions_removing_is_still_answered_as_a_question():
    assert propose_without_db("How much did I spend after removing VAT?", plan("not_edit")) is None


# --- Malformed or overreaching model output: no proposal, and the DB is never reached -----

@pytest.mark.parametrize("llm_reply", [
    "{not json",
    '{"status": "edit", "edits": [{"item": "rice", "field": "total", "new_value": "5"}]}',          # invalid field
    '{"status": "edit", "edits": [{"item": "rice", "field": ["quantity", "amount"], "new_value": "5"}]}',
    '{"status": "delete", "edits": []}',                                                           # unknown status
    '{"status": "edit", "edits": [{"receipt_id": "abc", "item": "rice", "field": "quantity", "new_value": "5"}]}',
    '{"status": "edit", "edits": [{"item": "rice", "field": "quantity"}]}',                       # no new value
    '{"status": "edit", "edits": {"item": "rice", "field": "quantity", "new_value": "5"}}',        # not a list
    '{"status": "edit", "operation": "delete", "edits": []}',                                     # unknown operation
    '{"status": "edit", "edits": [{"item": "rice", "field": "quantity", "new_value": "5", "line_item_id": 7}]}',
    '{"status": "edit", "edits": [{"item": "rice", "field": "quantity", "new_value": "5", "operation": "delete"}]}',
    '{"status": "edit", "edits": [{"item": "rice", "field": "quantity", "new_value": "5", "amount": "2500"}]}',
])
def test_malformed_or_overreaching_plans_never_reach_the_database(llm_reply):
    result = propose_without_db("Change rice quantity on receipt #1 to 5", llm_reply)
    assert result.edits == [] and result.message


@pytest.mark.parametrize("edits", [
    [{"receipt_id": 7, "item": "rice"}],                                                  # invented receipt
    [{"item": "rice", "line_no": 3}],                                                     # invented line
    [{"receipt_id": 1, "item": "rice"}, {"receipt_id": 1, "item": "oil"}],                # extra row
    [{"receipt_id": 1, "item": "rice"}, {"receipt_id": 1, "item": "rice", "field": "amount", "new_value": "9"}],
    [{"receipt_id": 1, "item": "rice", "new_value": "50"}],                               # value not asked for
    [{"item": f"rice {n}"} for n in range(config.MAX_EDITS_PER_REQUEST + 1)],             # bulk
])
def test_plans_that_go_beyond_the_request_never_reach_the_database(edits):
    result = propose_without_db("Change rice quantity on receipt #1 to 5", plan(edits=edits))
    assert result.edits == [] and result.message


# --- Shop / invoice grounding ------------------------------------------------------------

@pytest.mark.parametrize("merchants, invoices, edits, expected_merchant, problem", [
    (set(), set(), [req(item="rice", merchant="Shree")], "Shree", None),          # nothing named: unchanged
    ({"Ambikeshar Pharma"}, set(), [req(item="rice", merchant="ambikeshar pharma")], "Ambikeshar Pharma", None),
    ({"Ambikeshar Pharma"}, set(), [req(item="rice", merchant="Ambikeshar")], "Ambikeshar Pharma", None),
    ({"Ambikeshar Pharma", "Himalayan Pharma"}, set(), [req(item="rice", merchant="Ambikeshar Pharma")], None,
     "more than one shop"),
    ({"Ambikeshar Pharma"}, set(), [req(item="rice")], None, "wasn't sure"),         # model dropped the shop
    ({"Ambikeshar Pharma"}, set(), [req(item="rice", merchant="Himalayan")], None, "wasn't sure"),
    (set(), {"INV-1", "INV-2"}, [req(item="rice", invoice_number="INV-2")], None, "more than one invoice"),
])
def test_ground_bill_names(merchants, invoices, edits, expected_merchant, problem):
    grounded, message = edit.ground_bill_names(edits, merchants, invoices)
    if problem is None:
        assert message is None and grounded[0].merchant == expected_merchant
    else:
        assert grounded is None and problem in message


@pytest.fixture
def pharmacies(conn):
    """#1 and #2 Ambikeshar Pharma (Rice), #3 Himalayan Pharma (Rice), #4 Ambikeshar Ayurvedic Pharma (Honey)."""
    save(conn, "Ambikeshar Pharma", "AP-1", [_line("Rice", 2, 50000)])
    save(conn, "Ambikeshar Pharma", "AP-2", [_line("Rice", 3, 50000)])
    save(conn, "Himalayan Pharma", "HP-1", [_line("Rice", 1, 50000)])
    save(conn, "Ambikeshar Ayurvedic Pharma", "AAP-1", [_line("Honey", 1, 40000)])


@requires_test_db
def test_bill_names_in_finds_whole_names_only(pharmacies, conn):
    merchants, invoices = db.bill_names_in(conn, "Compare Ambikeshar Ayurvedic Pharma with HP-1 and AP-12")
    assert merchants == {"Ambikeshar Ayurvedic Pharma"} and invoices == {"HP-1"}


@requires_test_db
def test_same_merchant_on_two_receipts_asks_which(pharmacies, conn):
    result = propose("Change rice quantity to 5 for Ambikeshar Pharma",
                     [{"merchant": "Ambikeshar Pharma", "item": "rice", "new_value": "5"}])
    assert result.edits == [] and "more than one line" in result.message and proposal_statuses(conn) == []


@requires_test_db
@pytest.mark.parametrize("llm_merchant", ["Ambikeshar Pharma", "Himalayan Pharma"])
def test_two_shops_mentioned_asks_which_whatever_the_model_picks(pharmacies, conn, llm_merchant):
    question = ("I was comparing Ambikeshar Pharma with Himalayan Pharma. "
                "Change the rice quantity to 5 for Ambikeshar Pharma.")
    result = propose(question, [{"merchant": llm_merchant, "item": "rice", "new_value": "5"}])
    assert result.edits == [] and "more than one shop" in result.message and proposal_statuses(conn) == []


@requires_test_db
def test_one_shop_named_resolves_to_its_exact_ledger_name(pharmacies, conn):
    result = propose("Change honey quantity to 2 for Ambikeshar Ayurvedic Pharma",
                     [{"merchant": "Ambikeshar", "item": "honey", "new_value": "2"}])
    assert [(p.receipt_id, p.merchant_name, p.item) for p in result.edits] == [
        (4, "Ambikeshar Ayurvedic Pharma", "Honey")]


@requires_test_db
def test_misspelled_shop_is_not_fuzzy_matched(pharmacies, conn):
    result = propose("Change honey quantity to 2 for ambikehsar ayurvedic pharma",
                     [{"merchant": "ambikehsar ayurvedic pharma", "item": "honey", "new_value": "2"}])
    assert result.edits == [] and "couldn't find" in result.message and proposal_statuses(conn) == []


@requires_test_db
def test_two_invoices_mentioned_asks_which(ledger, conn):
    result = propose("I compared INV-1 and INV-2; change rice quantity on INV-2 to 5",
                     [{"invoice_number": "INV-2", "item": "rice", "new_value": "5"}])
    assert result.edits == [] and "more than one invoice" in result.message


@requires_test_db
def test_description_can_be_set_to_a_shop_like_name(ledger, conn):
    result = propose("Rename line 3 on receipt #1 to Ambik Pharma",
                     [{"receipt_id": 1, "line_no": 3, "field": "description", "new_value": "Ambik Pharma"}])
    assert approve(result).applied
    assert quantities(conn)[2] == (1, 3, "Ambik Pharma", 4.0)
    assert conn.execute("SELECT merchant_name FROM receipts WHERE id = 1").fetchone() == ("Shree Traders",)


# --- Session binding and proposal-id swapping ---------------------------------------------

@requires_test_db
def test_a_session_cannot_use_another_sessions_proposal_id(ledger, conn):
    before = tables(conn)
    mine = propose(*RICE_2_TO_5, session="session-a")
    theirs = propose("Set wheat quantity on receipt #1 to 6", [{"receipt_id": 1, "item": "wheat", "new_value": "6"}],
                     session="session-b")
    assert approve(mine, session="session-b").status == "unavailable"
    assert edit.cancel_edit(theirs.edits[0].proposal_id, "session-a", connect=connect).status == "unavailable"
    assert edit.apply_edit(str(uuid.uuid4()), "session-b", connect=connect).status == "unavailable"
    assert tables(conn) == before and proposal_statuses(conn) == ["pending", "pending"]


def test_approval_takes_no_target_or_value_from_the_caller():
    params = set(inspect.signature(edit.apply_edit).parameters)
    assert params == {"proposal_id", "session_id", "now", "today", "connect"}
    assert set(inspect.signature(edit.cancel_edit).parameters) == {"proposal_id", "session_id", "now", "connect"}


@requires_test_db
def test_tampering_with_every_card_field_changes_nothing_about_the_write(ledger, conn):
    result = propose(*RICE_2_TO_5)
    card = result.edits[0]
    for name, value in {"receipt_id": 2, "line_no": 3, "field": "description", "item": "Wheat",
                        "merchant_name": "Hari Stores", "invoice_number": "INV-2", "date_ad": date(2020, 1, 1),
                        "current_text": "4", "new_text": "999"}.items():
        setattr(card, name, value)
    assert approve(result).applied
    assert quantities(conn) == [(1, 1, "Rice", 5.0), (1, 2, "Rice", 3.0), (1, 3, "Wheat", 4.0), (2, 1, "Rice", 2.0)]


# --- Lifecycle: every terminal state is final -------------------------------------------

def _make_terminal(state, result, conn):
    pid = result.edits[0].proposal_id
    if state == "applied":
        assert approve(result).applied
    elif state == "cancelled":
        assert edit.cancel_edit(pid, SESSION, now=LATER, connect=connect).status == "cancelled"
    elif state == "stale":
        conn.execute("UPDATE line_items SET quantity = 4 WHERE receipt_id = 1 AND line_no = 1")
        assert approve(result).status == "stale"
    else:
        assert approve(result, now=NOW + TTL).status == "expired"


@requires_test_db
@pytest.mark.parametrize("state", ["applied", "cancelled", "stale", "expired"])
def test_terminal_states_are_final(ledger, conn, state):
    result = propose(*RICE_2_TO_5)
    _make_terminal(state, result, conn)
    after = tables(conn)
    assert proposal_statuses(conn) == [state]
    assert not approve(result).applied and not approve(result, now=NOW).applied
    cancelled = edit.cancel_edit(result.edits[0].proposal_id, SESSION, connect=connect)
    assert cancelled.status in ("already_decided", "expired")
    for target in ("pending", "applied", "cancelled", "stale", "expired"):
        if target != state:
            with pytest.raises(psycopg.errors.RaiseException, match="is final"):
                conn.execute("UPDATE line_item_edits SET status = %s, decided_at = now()", (target,))
    assert tables(conn) == after and proposal_statuses(conn) == [state]



# --- Planner cache: only replies that parse strictly are cached ---------------------------

GOOD_PLAN = plan(edits=[{"receipt_id": 1, "item": "rice"}])
OVERREACHING_PLAN = ('{"status": "edit", "edits": [{"receipt_id": 1, "item": "rice", "field": "quantity", '
                     '"new_value": "5", "operation": "delete"}]}')


def test_a_rejected_plan_is_not_cached_so_asking_again_reaches_the_model():
    question = "Change rice quantity on receipt #1 to 5"
    first, error = edit._get_plan(question, client=FakeClient(OVERREACHING_PLAN))
    assert first is None and "rephrasing" in error
    retry_client = FakeClient(GOOD_PLAN)
    second, error = edit._get_plan(question, client=retry_client)
    assert error is None and second.edits[0].item == "rice"
    assert len(retry_client.calls) == 1  # went to the model, not to a cached rejection


def test_a_valid_plan_is_cached():
    question = "Change rice quantity on receipt #1 to 5"
    edit._get_plan(question, client=FakeClient(GOOD_PLAN))
    replay, error = edit._get_plan(question, client=FakeClient())  # no outcomes queued: must be a cache hit
    assert error is None and replay.edits[0].item == "rice"


def test_a_previously_cached_rejected_plan_is_dropped_and_refetched():
    """An entry cached before this rule (lenient schema only) is no longer served."""
    question = "Change rice quantity on receipt #1 to 5"
    llm_client.call_json("x", namespace="edit", prompt_version=config.EDIT_PROMPT, cache_input=question,
                         response_schema=edit.EditPlan, client=FakeClient(OVERREACHING_PLAN))
    refetched, error = edit._get_plan(question, client=FakeClient(GOOD_PLAN))
    assert error is None and refetched.edits[0].item == "rice"



# --- Merchant-name changes: a specific refusal, and never a line description edit ----------

MERCHANT_RENAME = "edit the name of ambikeshar ayurvedic pharma to only ambik pharma"


@pytest.mark.parametrize("request_text, merchants", [
    ("change the merchant name of X to Y", set()),
    ("Change the shop name to Ambik Pharma", set()),
    (MERCHANT_RENAME, {"Ambikeshar Ayurvedic Pharma"}),
    ("Rename Ambikeshar Ayurvedic Pharma to Ambik Pharma", {"Ambikeshar Ayurvedic Pharma"}),
])
def test_merchant_name_changes_get_the_merchant_refusal(request_text, merchants):
    message = edit.refusal_for(request_text, merchants)
    assert "merchant/shop name" in message and "Edit on the Dashboard" in message
    assert "one at a time" not in message and "Bulk" not in message


@pytest.mark.parametrize("request_text, merchants", [
    ("Change the name of rice on the Shree Traders bill to Basmati", {"Shree Traders"}),  # an item's name
    ("Rename line 3 on receipt #1 to Ambikeshar Pharma", {"Ambikeshar Pharma"}),          # new description
    ("Change rice quantity to 5 for Ambikeshar Pharma", {"Ambikeshar Pharma"}),
])
def test_line_item_name_changes_are_not_merchant_refusals(request_text, merchants):
    assert edit.refusal_for(request_text, merchants) is None


@requires_test_db
@pytest.mark.parametrize("status, llm_edits", [
    ("unsupported", []),                                                   # how Gemini classifies it
    ("edit", [{"item": "ambikeshar ayurvedic pharma", "field": "description", "new_value": "ambik pharma"}]),
    ("edit", [{"merchant": "ambikeshar ayurvedic pharma", "item": "ambikeshar ayurvedic pharma",
               "field": "description", "new_value": "only ambik pharma"}]),
    ("ambiguous", []),
])
def test_merchant_rename_is_refused_and_never_becomes_a_description_edit(pharmacies, conn, status, llm_edits):
    # Even a line whose description contains the shop's name can't be picked up.
    save(conn, "Ambikeshar Ayurvedic Pharma", "AAP-2", [_line("Ambikeshar Ayurvedic Pharma tonic", 1, 30000)])
    before = tables(conn)
    result = propose(MERCHANT_RENAME, llm_edits, status=status)
    assert result.edits == []
    assert result.message.startswith("I can’t change the merchant/shop name from chat.")
    assert "Edit on the Dashboard" in result.message
    assert tables(conn) == before and proposal_statuses(conn) == []
    assert conn.execute("SELECT count(*) FROM line_items WHERE description ILIKE '%ambik pharma%'").fetchone() == (0,)


def test_merchant_rename_refusal_does_not_need_the_database_when_it_says_merchant():
    result = propose_without_db("change the merchant name of ambikehsar ayurvedic pharma to ambik pharma",
                                plan("unsupported"))
    assert result.edits == [] and "merchant/shop name" in result.message
