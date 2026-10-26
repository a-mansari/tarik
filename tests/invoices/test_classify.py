"""A draft taken up again keeps the side it was classified as: the model's second look must not flip it."""

import pytest

from engine.tools.invoices import classify


@pytest.fixture
def asked(db_cabinet, monkeypatch):
    """classify_invoice on a purchase draft 5: what the model answers, how often it was asked, the type changes."""
    state = {"answer": "vente", "model": 0, "filled": []}
    draft = {"state": "draft", "type": "purchase", "company_id": [1, "D"], "exercice_id": False, "document_pdf": ""}
    read = ("detected_supplier_ice", "detected_customer_ice", "detected_supplier_if", "detected_customer_if")
    draft.update(dict.fromkeys(read, False), folder_id=False, folder_type=False)
    state["draft"], state["company"] = draft, {"name": "ALL FWD MAROC", "ice": "003273903000040", "vat": "53781112"}
    monkeypatch.setattr(classify, "odoo_call", lambda model, method, ids, fields: [state["company"]])
    monkeypatch.setattr(classify, "require_invoice", lambda i, f: {"id": i, **draft})
    monkeypatch.setattr(classify, "still_reading", lambda invoice: None)
    monkeypatch.setattr(classify, "has_scan", lambda invoice: True)
    monkeypatch.setattr(classify, "scan_pages", lambda pdf: [])
    monkeypatch.setattr(classify, "require_draft", lambda *a: None)
    monkeypatch.setattr(classify, "invoice_context", lambda invoice: {})
    monkeypatch.setattr(classify, "test_mode_note", lambda: "")
    monkeypatch.setattr(classify, "fill_invoice", lambda invoice_id, values, context: state["filled"].append(values))

    def model(pages, what, dossier=""):
        state["model"] += 1
        state["told"] = dossier
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


@pytest.mark.functionality
def test_a_document_the_client_gave_as_a_purchase_is_one_even_when_the_model_doubts(asked):
    asked["answer"] = "autre"  # a customs liquidation slip
    result = classify.classify_invoice(5, "achat")
    assert result["category"] == "achat" and result["status"] != "stop"


@pytest.mark.functionality
def test_the_side_experios_reading_proves_by_the_dossiers_ice_wins_over_the_model(asked):
    """ALL FWD MAROC's own sale, read by the model as a purchase (2026-10-24): the issuer Experio read carries the
    dossier's ICE, so it is a sale, as Experio's own ICE warning says."""
    asked["answer"] = "achat"
    asked["draft"]["detected_supplier_ice"] = "003273903000040"
    result = classify.classify_invoice(5, "vente")
    assert result["category"] == "vente" and asked["model"] == 0 and asked["filled"] == [{"type": "sale"}]
    asked["draft"].update(detected_supplier_ice="009", detected_customer_if="53781112")  # the buyer is the dossier
    assert classify.classify_invoice(5)["category"] == "achat" and asked["model"] == 0


@pytest.mark.functionality
def test_the_model_is_told_whose_dossier_it_is(asked):
    asked["answer"] = "vente"
    classify.classify_invoice(5)
    assert asked["told"] == "ALL FWD MAROC"


@pytest.mark.functionality
def test_an_import_folders_piece_is_a_purchase_without_asking_the_model(asked):
    asked["answer"] = "autre"  # a customs liquidation slip: the model doubts it is an invoice
    asked["draft"].update(folder_id=[4, "2601"], folder_type="import", type="sale")
    result = classify.classify_invoice(5)
    assert result["category"] == "achat" and asked["model"] == 0 and asked["filled"] == [{"type": "purchase"}]


@pytest.mark.functionality
def test_a_draft_experio_still_reads_or_without_a_scan_is_not_classified(asked, monkeypatch):
    monkeypatch.setattr(classify, "still_reading", lambda invoice: {"status": "wait", "message": "lecture"})
    assert classify.classify_invoice(5)["status"] == "wait"
    monkeypatch.setattr(classify, "still_reading", lambda invoice: None)
    monkeypatch.setattr(classify, "has_scan", lambda invoice: False)
    assert classify.classify_invoice(5)["message"] == "Facture 5 : pas de scan PDF a classer."
    assert asked["model"] == 0 and asked["filled"] == []


@pytest.mark.functionality
def test_a_bank_statement_is_stopped_and_written_nowhere(asked):
    asked["answer"] = "rb"
    result = classify.classify_invoice(5)
    assert result["status"] == "stop" and result["category"] == "rb" and "un relevé bancaire" in result["message"]
    assert asked["filled"] == []


@pytest.mark.functionality
def test_a_scan_of_the_other_side_than_the_client_said_stops_untouched(asked):
    asked["answer"] = "vente"
    result = classify.classify_invoice(5, "achat")
    assert result["status"] == "stop" and "vous l'avez donnée comme achat" in result["message"]
    assert asked["filled"] == []


@pytest.mark.functionality
def test_a_draft_experio_already_has_on_the_right_side_is_left_as_it_is(asked):
    asked["answer"] = "achat"
    result = classify.classify_invoice(5)
    assert result["routine"] and "déjà enregistrée comme telle" in result["message"] and asked["filled"] == []


@pytest.mark.functionality
def test_the_type_is_changed_through_experios_form_in_the_invoices_dossier(asked, monkeypatch):
    contexts = []
    monkeypatch.setattr(classify, "invoice_context", lambda invoice: contexts.append(invoice["id"]) or {"c": 1})
    monkeypatch.setattr(
        classify,
        "fill_invoice",
        lambda invoice_id, values, context: asked["filled"].append((invoice_id, values, context)),
    )
    assert classify.classify_invoice(5)["message"] == "Facture 5 : vente (why), type change en sale."
    assert asked["filled"] == [(5, {"type": "sale"}, {"c": 1})] and contexts == [5]


@pytest.mark.security
def test_test_mode_says_the_type_would_change(asked, monkeypatch):
    monkeypatch.setattr(classify, "test_mode_note", lambda: "MODE TEST. ")
    assert classify.classify_invoice(5)["message"] == "MODE TEST. Facture 5 : vente (why), le type passerait a sale."


@pytest.mark.communication
def test_a_type_change_experio_refuses_is_said_refused(asked, monkeypatch):
    def refuse(*args):
        raise RuntimeError("ValidationError: facture verrouillée")

    monkeypatch.setattr(classify, "fill_invoice", refuse)
    with pytest.raises(classify.ToolError, match="Facture 5 : Experio a refusé le changement de type."):
        classify.classify_invoice(5)


@pytest.mark.security
def test_a_draft_no_longer_editable_gets_no_type_change(asked, monkeypatch):
    def posted(*args):
        raise classify.ToolError("Facture 5 : déjà comptabilisée.")

    monkeypatch.setattr(classify, "require_draft", posted)
    with pytest.raises(classify.ToolError, match="déjà comptabilisée"):
        classify.classify_invoice(5)
    assert asked["filled"] == []


@pytest.mark.technical
def test_a_category_the_model_invents_is_refused(monkeypatch):
    told = []
    monkeypatch.setattr(
        classify, "ask_model", lambda system, pages, prompt, what: told.append(prompt) or {"category": "x"}
    )
    with pytest.raises(classify.ToolError, match="n'a pas su dire si c'est une facture d'achat"):
        classify.classify_pages([], "Classement", "ALPHA")
    monkeypatch.setattr(classify, "ask_model", lambda *a: {"category": "vente", "reason": None})
    assert classify.classify_pages([], "Classement") == ("vente", "")
    assert told[0].startswith("The cabinet's client here is « ALPHA »")


@pytest.mark.functionality
def test_an_invoices_side_is_its_classification_else_experios_type(db_cabinet):
    assert classify.side_of({"id": 5, "type": "sale"}) == "sale"
    with pytest.raises(classify.ToolError, match="ni achat ni vente"):
        classify.side_of({"id": 5, "type": "out_refund"})
    classify._save_classification(5, "achat", "why")
    assert classify.side_of({"id": 5, "type": "sale"}) == "purchase"
    classify._save_classification(5, "rb", "why")
    with pytest.raises(classify.ToolError, match="ce n'est pas une facture"):
        classify.side_of({"id": 5, "type": "purchase"})


@pytest.mark.security
@pytest.mark.parametrize(
    ("side", "words", "kept"),
    [
        ("achat", "mes factures fournisseurs", "achat"),
        ("vente", "voici mes factures clients", "vente"),
        ("mixte", "achats et ventes du mois", "mixte"),
        ("mixte", "achats du mois", None),
        ("vente", "voici les factures", None),
        ("autre", "achats", None),
    ],
)
def test_a_side_is_taken_only_when_the_client_wrote_it(side, words, kept):
    assert classify.client_side(side, words) == kept
