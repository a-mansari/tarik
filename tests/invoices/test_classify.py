"""A draft taken up again keeps the side it was classified as: the model's second look must not flip it."""

import pytest

from tarik.invoices import classify


@pytest.fixture
def asked(db_cabinet, monkeypatch):
    """classify_invoice on a purchase draft 5: what the model answers, how often it was asked, the type changes."""
    state = {"answer": "vente", "model": 0, "filled": []}
    draft = {"state": "draft", "type": "purchase", "company_id": [1, "D"], "exercice_id": False, "document_pdf": ""}
    monkeypatch.setattr(classify, "require_invoice", lambda i, f: {"id": i, **draft})
    monkeypatch.setattr(classify, "still_reading", lambda invoice: None)
    monkeypatch.setattr(classify, "has_scan", lambda invoice: True)
    monkeypatch.setattr(classify, "scan_pages", lambda pdf: [])
    monkeypatch.setattr(classify, "require_draft", lambda *a: None)
    monkeypatch.setattr(classify, "invoice_context", lambda invoice: {})
    monkeypatch.setattr(classify, "test_mode_note", lambda: "")
    monkeypatch.setattr(classify, "fill_invoice", lambda invoice_id, values, context: state["filled"].append(values))

    def model(pages, what):
        state["model"] += 1
        return state["answer"], "why"

    monkeypatch.setattr(classify, "classify_pages", model)
    return state


@pytest.mark.functionality
def test_a_second_look_does_not_flip_a_classified_draft(asked):
    asked["answer"] = "achat"
    assert classify.classify_invoice(5)["category"] == "achat"
    asked["answer"] = "vente"  # the model changes its mind when the draft is taken up again
    result = classify.classify_invoice(5)
    assert result["category"] == "achat" and asked["model"] == 1 and asked["filled"] == []


@pytest.mark.functionality
def test_a_side_the_client_gives_asks_for_a_new_look(asked):
    asked["answer"] = "achat"
    classify.classify_invoice(5)
    asked["answer"] = "vente"
    assert classify.classify_invoice(5, "vente")["category"] == "vente" and asked["model"] == 2


@pytest.mark.functionality
def test_a_draft_that_is_no_invoice_is_left_alone(asked):
    asked["answer"] = "autre"  # a screenshot, a contract: never given a side nor a type
    result = classify.classify_invoice(5)
    assert result["status"] == "stop" and "ce n'est pas une facture mais un autre document" in result["message"]
    assert asked["filled"] == []
