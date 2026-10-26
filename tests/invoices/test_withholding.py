"""Withholding answers: read by the model, checked here against what Experio takes, and how they read back."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import withholding
from engine.tools.invoices.withholding import described, local_withholding_answer, withholding_answer


@pytest.fixture
def model(monkeypatch):
    """The model's reading, set by each test; the client's own answer reaches it word for word."""
    reading, seen = {}, []
    monkeypatch.setattr(
        withholding, "ask_model", lambda system, pages, instruction, what: seen.append(instruction) or dict(reading)
    )
    return reading, seen


@pytest.mark.functionality
def test_foreign_withholding(model):
    reading, seen = model
    reading.update(rule="society", abroad=True)
    assert withholding_answer("a la charge de la societe, contrat 2025, hors Maroc") == {
        "retenue": True,
        "foreign_withholding": "society",
        "local_service": False,
    }
    assert "a la charge de la societe, contrat 2025, hors Maroc" in seen[0]
    reading.update(rule="none", abroad=False)
    assert withholding_answer("sans")["retenue"] is False
    reading.update(rule="maybe")
    assert withholding_answer("je ne sais pas") is None  # nothing Experio takes: asked again
    reading.update(rule="supplier", abroad=True)
    assert (
        described(withholding_answer("2, hors maroc"))
        == "retenue étrangère à la charge du fournisseur, service rendu hors Maroc"
    )


@pytest.mark.functionality
def test_local_withholding(model):
    reading, _ = model
    reading.update(retenue=True, is_ir=10, tva=75)
    assert local_withholding_answer("10 et 75") == {
        "retenue": True,
        "local_withholding_percentage": "10",
        "tax_withholding_percentage": "75",
    }
    assert described(local_withholding_answer("10 et 75")) == "retenue à la source IS/IR 10 % et TVA 75 %"
    reading.update(retenue=True, is_ir="10%", tva="75 %")
    assert local_withholding_answer("10 % et 75 %")["local_withholding_percentage"] == "10"
    assert local_withholding_answer("10 % et 75 %")["tax_withholding_percentage"] == "75"
    reading.update(retenue=False)
    assert local_withholding_answer("sans, facture du 15/03")["retenue"] is False


@pytest.mark.security
@pytest.mark.parametrize(
    "reading",
    [
        {"retenue": True, "is_ir": 15.5, "tva": 0},
        {"retenue": True, "is_ir": 12, "tva": 0},
        {"retenue": True, "is_ir": "dix", "tva": 75},
        {"retenue": True, "is_ir": 10, "tva": 50},
        {"retenue": True, "is_ir": None, "tva": 0},
        {"retenue": None, "is_ir": 10, "tva": 0},
        {"retenue": "oui", "is_ir": 10},
    ],
)
def test_a_rate_experio_does_not_take_is_never_written(model, reading):
    model[0].update(reading)
    assert local_withholding_answer("x") is None


@pytest.fixture
def local(monkeypatch):
    """A local purchase in a fake Experio: its supplier, the supplier's attestations, the dossier, every write."""
    state = {
        "invoice": {
            "id": 3,
            "state": "draft",
            "type": "purchase",
            "is_local_invoice": True,
            "company_id": [5, "D"],
            "exercice_id": False,
            "partner_id": [7, "BTP SARL"],
            "account_id": [1, "61330000 Entretien"],
            "invoice_date": "2026-10-15",
            "retenue": False,
            "foreign_withholding": False,
            "local_service": True,
            "local_withholding_percentage": False,
            "tax_withholding_percentage": "0",
        },
        "partner": {"id": 7, "name": "BTP SARL", "type": "company", "expense_type": "travaux"},
        "certificates": [],
        "public": False,
        "over_thresholds": False,
        "written": [],
        "created": [],
    }
    monkeypatch.setattr(withholding, "require_invoice", lambda i, f: dict(state["invoice"]))
    monkeypatch.setattr(withholding, "dry_run", lambda: False)
    monkeypatch.setattr(withholding, "refresh", lambda i, context: None)
    monkeypatch.setattr(withholding, "test_mode_note", lambda: "")

    def odoo_call(model, method, *args, **kw):
        if model == "exp.partner":
            return [dict(state["partner"])]
        if model == "res.company":
            return [{"exp_is_public_organization": state["public"]}]
        if method == "create":
            state["created"].append(args[0])
            return 41
        return [{"state_msg": False, **c} for c in state["certificates"]]

    def fill_invoice(invoice_id, changes, context):
        state["written"].append(changes)
        return {"tax_withholding_percentage": state.get("experio_rate", "100")} if changes.get("retenue") else {}

    monkeypatch.setattr(withholding, "odoo_call", odoo_call)
    monkeypatch.setattr(withholding, "odoo_json", lambda model, method, ids: state["over_thresholds"])
    monkeypatch.setattr(withholding, "fill_invoice", fill_invoice)
    return state


@pytest.mark.functionality
def test_a_supplier_with_an_activity_type_gets_its_retenue_on_and_experio_sets_the_rate(db_cabinet, local):
    local["certificates"] = [
        {
            "regularity_date": "2026-06-01",
            "regularity_end_date": "2026-12-01",
            "state": "active",
            "bluejay_state": "done",
        }
    ]
    local["experio_rate"] = "0"
    result = withholding.settle_withholding(3)
    assert local["written"] == [{"retenue": True}]  # never the rate itself: Experio's onchange sets it
    assert result["message"] == "Facture 3 : retenue à la source activée, à 0 % (activité du fournisseur : travaux)."


@pytest.mark.functionality
def test_no_activity_type_no_retenue_and_none_before_july_2024(db_cabinet, local):
    local["partner"]["expense_type"] = False
    assert withholding.settle_withholding(3)["routine"] and local["written"] == []
    local["partner"]["expense_type"] = "travaux"
    local["invoice"]["invoice_date"] = "2024-06-30"  # Experio's RAS TVA applies from 2024-07-01
    assert withholding.settle_withholding(3)["routine"] and local["written"] == []


@pytest.mark.functionality
def test_the_attestation_is_asked_once_per_supplier_when_the_rate_depends_on_it(db_cabinet, local):
    asked = withholding.settle_withholding(3)
    assert asked["status"] == "question" and "attestation de régularité fiscale" in asked["message"]
    assert local["written"] == []
    # « sans attestation »: Experio's rate without one, and the supplier is not asked again.
    result = withholding.settle_withholding(3, "Il n'a pas d'attestation, sans attestation", answering=True)
    assert result["status"] == "ok" and "TVA 100 %" in result["message"]
    assert withholding.settle_withholding(3)["status"] == "ok"  # not asked again for this supplier
    # The rate doesn't depend on it: never asked (a public dossier's works, a company's services).
    with db_cabinet_cleared():
        local["public"] = True
        assert withholding.settle_withholding(3)["status"] == "ok"


def db_cabinet_cleared():
    """The test cabinet's « sans attestation » notes cleared."""
    import contextlib

    from engine.tools.core.cabinets import cabinet_db

    @contextlib.contextmanager
    def cleared():
        with cabinet_db() as db:
            db.execute("delete from tarik_no_certificate")
        yield

    return cleared()


@pytest.mark.functionality
def test_an_attestation_sent_as_the_answer_is_uploaded_to_the_supplier_then_awaited(db_cabinet, local):
    from engine.tools.core.cabinets import cabinet_db
    from engine.tools.invoices import pipeline

    pipeline.record_step("r", "s", "settle_withholding", {**withholding.settle_withholding(3), "invoice_id": 3})
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, created_at) values "
            "('s', 'attestation.pdf', 'application/pdf', %s, now() + interval '1 second')",
            (b"%PDF-1.4 attestation",),
        )
    result = withholding.settle_withholding(3, "ci-joint", answering=True)
    assert result["status"] == "wait" and "« attestation.pdf »" in result["message"]
    assert local["created"][0]["partner_id"] == 7 and local["created"][0]["document_pdf_name"] == "attestation.pdf"
    with cabinet_db() as db:
        assert db.execute("select kind, regularity_id from tarik_files").fetchone() == ("certificate", 41)
        assert db.execute(pipeline.Q.files_to_process, ("s",)).fetchall() == []  # never taken for an invoice
    local["certificates"] = [
        {"regularity_date": False, "regularity_end_date": False, "state": "pending", "bluejay_state": "pending"}
    ]
    assert withholding.settle_withholding(3)["status"] == "wait"  # Experio still reads it
    local["certificates"][0].update(
        regularity_date="2026-01-01", regularity_end_date="2026-07-01", state="expired", bluejay_state="done"
    )
    assert withholding.settle_withholding(3)["status"] == "question"  # read, but no longer valid: asked again
    local["certificates"][0].update(state="error", state_msg="Vérification échouée \n - Date illisible.")
    asked = withholding.settle_withholding(3)  # refused by Experio: its reason told
    assert "n'a pas été validée par Experio (Vérification échouée - Date illisible.)" in asked["message"]


@pytest.mark.functionality
def test_a_persons_services_need_it_only_over_experios_thresholds(db_cabinet, local):
    local["partner"].update(type="person", expense_type="prestation")
    assert withholding.settle_withholding(3)["status"] == "ok"  # under 5 000 and the month under 50 000
    local["written"].clear()
    local["over_thresholds"] = True
    assert withholding.settle_withholding(3)["status"] == "question"


@pytest.mark.functionality
def test_an_is_ir_rate_the_tiers_type_does_not_take_is_asked_again(db_cabinet, local, model):
    local["partner"].update(expense_type=False)
    local["invoice"]["account_id"] = [1, "61360000 Honoraires"]
    assert "IS/IR" in withholding.settle_withholding(3)["message"]  # fees: asked their IS/IR rate
    model[0].update(retenue=True, is_ir=15, tva=0)
    asked = withholding.settle_withholding(3, "15", answering=True)
    assert asked["status"] == "question" and "personne morale (Experio prend 5, 10, 20, 30 %)" in asked["message"]
    model[0].update(retenue=True, is_ir=10, tva=75)
    result = withholding.settle_withholding(3, "10 et 75", answering=True)
    assert local["written"][-1] == {"retenue": True, "local_withholding_percentage": "10"}  # 75: Experio's to set
    assert result["status"] == "ok"


@pytest.mark.functionality
def test_a_sale_gets_its_retenue_on_when_the_customer_has_an_activity_type(db_cabinet, local):
    local["invoice"].update(type="sale", partner_id=[8, "CLIENT SA"], account_id=[2, "71240000 Services"])
    local["partner"].update(id=8, name="CLIENT SA", type="company", expense_type="prestation")
    local["experio_rate"] = "0"
    result = withholding.settle_withholding(3)
    assert local["written"] == [{"retenue": True}]  # its rate: Experio's rules (a company's services: 0 %)
    assert result["message"].endswith("(activité du client : prestation de services).")
    local["written"].clear()
    local["partner"]["expense_type"] = False  # no activity type: nothing turned on, nothing asked
    assert withholding.settle_withholding(3)["routine"] and local["written"] == []
    local["partner"].update(expense_type="travaux")  # its rate depends on the customer's attestation: asked once
    assert withholding.settle_withholding(3)["status"] == "question"


@pytest.fixture
def foreign(local, monkeypatch):
    """The same purchase from a foreign supplier, its last posted foreign rule (``last``) in Experio, every search."""
    local["invoice"].update(is_local_invoice=False, partner_id=[7, "ACME LTD"], account_id=[1, "61360000 Honoraires"])
    local.update(last=[], searched=[])
    fake = withholding.odoo_call

    def odoo_call(model, method, *args, **kw):
        if (model, method) == ("exp.invoice", "search_read"):
            local["searched"].append(args[0])
            return local["last"]
        return fake(model, method, *args, **kw)

    monkeypatch.setattr(withholding, "odoo_call", odoo_call)
    return local


@pytest.mark.functionality
def test_without_its_tiers_there_is_no_withholding_to_settle(local):
    local["invoice"]["partner_id"] = False
    assert withholding.settle_withholding(3)["routine"] and local["written"] == []


@pytest.mark.functionality
def test_a_foreign_suppliers_rule_experio_applied_is_only_said(foreign):
    foreign["invoice"].update(retenue=True, foreign_withholding="supplier")
    result = withholding.settle_withholding(3)
    assert result["message"] == "Facture 3 : retenue étrangère à la charge du fournisseur (règle connue du tiers)."
    assert foreign["written"] == [] and foreign["searched"] == []


@pytest.mark.functionality
def test_a_foreign_suppliers_last_posted_rule_is_written_again(foreign):
    foreign["last"] = [{"retenue": True, "foreign_withholding": "society", "local_service": True, "id": 2}]
    result = withholding.settle_withholding(3)
    domain = foreign["searched"][0]
    assert ("partner_id", "=", 7) in domain and ("id", "!=", 3) in domain and ("is_local_invoice", "=", False) in domain
    assert foreign["written"] == [{"retenue": True, "foreign_withholding": "society", "local_service": True}]
    assert result["message"] == "Facture 3 : retenue étrangère à la charge de la société."


@pytest.mark.communication
def test_a_foreign_supplier_with_no_known_rule_is_asked_and_its_answer_written(foreign, model):
    asked = withholding.settle_withholding(3)
    assert asked["status"] == "question" and asked["message"].startswith("Facture de `ACME LTD` : quelle retenue")
    model[0].update(rule="maybe")
    unclear = withholding.settle_withholding(3, "on verra", answering=True)
    assert unclear["status"] == "question" and "réponse non comprise" in unclear["message"]
    assert foreign["written"] == []
    model[0].update(rule="none", abroad=False)
    result = withholding.settle_withholding(3, "sans", answering=True)
    assert foreign["written"] == [{"retenue": False, "foreign_withholding": "none", "local_service": True}]
    assert result["message"] == "Facture 3 : sans retenue à la source."


@pytest.mark.security
def test_a_withholding_the_client_did_not_write_is_never_taken_from_their_message(foreign, model):
    model[0].update(rule="society", abroad=False)
    asked = withholding.settle_withholding(3, "à la charge de la société", client_message="Voici la facture d'ACME.")
    assert asked["status"] == "question" and foreign["written"] == []
    withholding.settle_withholding(3, "à la charge de la société", client_message="RAS à la charge de la société.")
    assert foreign["written"] == [{"retenue": True, "foreign_withholding": "society", "local_service": True}]


@pytest.mark.functionality
def test_experio_refusing_the_withholding_is_a_tool_error(foreign, monkeypatch):
    foreign["last"] = [{"retenue": True, "foreign_withholding": "society", "local_service": True}]

    def fill_invoice(invoice_id, changes, context):
        raise RuntimeError("Traceback\nValidationError: Compte RAS manquant")

    monkeypatch.setattr(withholding, "fill_invoice", fill_invoice)
    with pytest.raises(ToolError, match="Experio a refusé la retenue à la source"):
        withholding.settle_withholding(3)


@pytest.mark.communication
def test_an_unclear_is_ir_answer_is_asked_again(local, model):
    local["partner"]["expense_type"] = False
    local["invoice"]["account_id"] = [1, "61360000 Honoraires"]
    model[0].update(retenue=None)
    asked = withholding.settle_withholding(3, "je vérifie", answering=True)
    assert asked["status"] == "question" and "réponse non comprise, y a-t-il une retenue" in asked["message"]
    assert local["written"] == []


@pytest.mark.functionality
def test_sans_is_ir_keeps_the_retenue_on_for_experios_ras_tva(local, model):
    local["certificates"] = [
        {
            "regularity_date": "2026-01-01",
            "regularity_end_date": "2027-01-01",
            "state": "active",
            "bluejay_state": "done",
        }
    ]
    model[0].update(retenue=False)
    withholding.settle_withholding(3, "sans", answering=True)
    assert local["written"] == [{"retenue": True, "local_withholding_percentage": False}]


@pytest.mark.functionality
def test_a_known_rule_with_nothing_to_write_is_only_said(local):
    local["partner"]["expense_type"] = False
    local["invoice"].update(retenue=True, local_withholding_percentage="10")
    result = withholding.settle_withholding(3)
    assert result["message"] == "Facture 3 : retenue à la source IS/IR 10 % (règle connue du tiers)."
    local["invoice"].update(type="sale", account_id=[2, "71240000 Services"])
    result = withholding.settle_withholding(3)
    assert result["message"] == "Facture 3 : retenue à la source IS/IR 10 % (règle connue du client)."
    assert local["written"] == []


@pytest.mark.functionality
def test_in_test_mode_an_attestation_sent_is_awaited_never_uploaded(db_cabinet, local, monkeypatch):
    from engine.tools.core.cabinets import cabinet_db
    from engine.tools.invoices import pipeline

    monkeypatch.setattr(withholding, "dry_run", lambda: True)
    pipeline.record_step("r", "s", "settle_withholding", {**withholding.settle_withholding(3), "invoice_id": 3})
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, created_at) values "
            "('s', 'attestation.pdf', 'application/pdf', %s, now() + interval '1 second')",
            (b"%PDF-1.4 attestation",),
        )
    result = withholding.settle_withholding(3, "ci-joint", answering=True)
    assert result["status"] == "wait" and "« attestation.pdf »" in result["message"]
    assert local["created"] == []
