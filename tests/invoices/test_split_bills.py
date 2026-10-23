"""A document holding several bills (water and electricity): Experio makes one draft per bill, Tarik takes each in
hand and checks each against its own bill only."""

import pytest
from langgraph.graph import END

from engine import traitement
from tarik.invoices import check

ELECTRICITY = {
    "id": 272378,
    "token": "job-1",
    "utility_type": "electricity",
    "invoice_number": "1622395000 MW",
    "document_pdf": "JVBERi0=",
    "file_type": "pdf",
}
WATER = {"id": 272379, "document_pdf": False, "file_type": "none"}


@pytest.mark.functionality
def test_each_draft_split_off_runs_its_steps_once():
    state = {"call": {}, "run_id": "r", "outcomes": [{"invoice_id": 272378, "split_off": [272379]}]}
    sent = traitement.split_drafts(state)
    assert [(s.node, s.arg["invoice_id"], s.arg["entry"]) for s in sent] == [("facture", 272379, "wait_until_read")]
    state["outcomes"].append({"invoice_id": 272379, "status": "ok"})  # its steps ran
    assert traitement.split_drafts(state) == END
    assert traitement.split_drafts({**state, "outcomes": [{"invoice_id": 1, "status": "ok"}]}) == END


@pytest.mark.functionality
def test_a_split_draft_is_checked_against_its_own_bill_on_the_documents_scan(monkeypatch):
    monkeypatch.setattr(check, "odoo_call", lambda *a, **k: [WATER])
    scanned, part = check.split_scan(ELECTRICITY)
    assert scanned is ELECTRICITY and "only the electricity bill numbered 1622395000 MW" in part
    assert "never with another bill" in part
    water = {**WATER, "token": "job-1", "utility_type": "water", "invoice_number": "1622365185 GQ"}
    monkeypatch.setattr(check, "odoo_call", lambda *a, **k: [ELECTRICITY])
    scanned, part = check.split_scan(water)
    assert scanned is ELECTRICITY and "only the water bill numbered 1622365185 GQ" in part  # its sibling's scan


@pytest.mark.functionality
def test_a_draft_alone_of_its_document_is_checked_as_before(monkeypatch):
    monkeypatch.setattr(check, "odoo_call", lambda *a, **k: [])
    assert check.split_scan(ELECTRICITY) == (None, "")
    assert check.split_scan({**ELECTRICITY, "token": False}) == (None, "")
