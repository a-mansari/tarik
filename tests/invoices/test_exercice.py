"""Exercice choice, read in code, never by a model."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.invoices import exercice
from engine.tools.invoices.exercice import exercice_choice, given_date

OPENS = [
    {"id": 1, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"},
    {"id": 2, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"},
]


@pytest.mark.functionality
def test_the_agents_choice_is_taken_only_among_the_open_exercices():
    assert exercice_choice("Exercice 2025", OPENS) == ("old", OPENS[1])  # named as the question lists it
    assert exercice_choice(str(OPENS[0]["id"]), OPENS) == ("old", OPENS[0])
    assert exercice_choice("nouveau", OPENS) == exercice_choice(" Nouveau ", OPENS) == ("new", None)
    for unclear in ("", "Exercice 2023", "2025", "cree un nouvel exercice", "le premier"):
        assert exercice_choice(unclear, OPENS) is None, unclear  # the client is asked again


@pytest.mark.functionality
def test_the_date_given_is_one_full_date():
    assert given_date("du 15/03/2025") == "2025-03-15" and given_date("2025-03-15") == "2025-03-15"
    assert given_date("ref 12-3-45 le 15/03/2025") == "2025-03-15"  # a reference is no date
    assert (
        given_date("le 15/03/25") is None and given_date("15/03/2025 ou 16/03/2025") is None and given_date("") is None
    )


@pytest.mark.functionality
def test_an_exercice_covering_the_date_is_linked_whatever_the_answer(monkeypatch):
    """The cabinet opened the year by hand between the question and the answer: the invoice goes there."""
    from engine.tools.invoices import exercice

    invoice = {
        "state": "draft",
        "company_id": [5, "D"],
        "exercice_id": False,
        "invoice_date": "2025-03-15",
        "is_old_invoice": False,
    }
    monkeypatch.setattr(exercice, "require_draft_invoice", lambda invoice_id, fields, what: dict(invoice))
    monkeypatch.setattr(exercice, "odoo_call", lambda *args, **kw: OPENS)
    monkeypatch.setattr(
        exercice, "relink", lambda invoice_id, invoice, found: {"status": "ok", "message": found["name"]}
    )
    assert exercice.settle_exercice(3, answer="nouveau", decision="nouveau")["message"] == "Exercice 2025"


INVOICE = {
    "state": "draft",
    "company_id": [5, "D"],
    "exercice_id": False,
    "invoice_date": "2023-06-10",
    "is_old_invoice": False,
}
REFUSAL = "Traceback\nValidationError: Chevauchement d'exercices"


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the dossier's open exercices, its late-invoice account, every call and write; the methods
    named in ``refuse`` (or "fill", the form) refused."""
    state = {"invoice": dict(INVOICE), "calls": [], "written": [], "dry": False, "refuse": set()}
    state["company"] = {"old_invoice_account_id": [1, "61800000 Factures antérieures"]}
    monkeypatch.setattr(exercice, "require_draft_invoice", lambda invoice_id, fields, what: dict(state["invoice"]))
    monkeypatch.setattr(exercice, "dry_run", lambda: state["dry"])
    monkeypatch.setattr(exercice, "test_mode_note", lambda: TEST_MODE if state["dry"] else "")

    def odoo_call(model, method, *args, **kw):
        state["calls"].append((model, method))
        return [state["company"]] if model == "res.company" else OPENS

    def odoo_json(model, method, values, context):
        state["calls"].append((model, method, values, context))
        if method in state["refuse"]:
            raise RuntimeError(REFUSAL)
        return 61 if method == "create" else None

    def fill_invoice(invoice_id, changes, context):
        if "fill" in state["refuse"]:
            raise RuntimeError(REFUSAL)
        state["written"].append((changes, context))
        return changes

    monkeypatch.setattr(exercice, "odoo_call", odoo_call)
    monkeypatch.setattr(exercice, "odoo_json", odoo_json)
    monkeypatch.setattr(exercice, "fill_invoice", fill_invoice)
    return state


def json_calls(state):
    return [call[1:] for call in state["calls"] if len(call) == 4]


@pytest.mark.functionality
def test_a_new_exercice_is_created_by_experio_then_opened_by_its_own_action(experio):
    assert exercice.open_exercice(5, "2023") == 61
    values = {"company_id": 5, "date_start": "2023-01-01", "date_end": "2023-12-31", "name": "Exercice 2023"}
    assert json_calls(experio) == [
        ("create", values, {"allowed_company_ids": [5]}),
        ("action_open", [61], {"allowed_company_ids": [5]}),
    ]


@pytest.mark.functionality
def test_nouveau_opens_the_invoices_year_and_links_the_invoice_to_it(experio):
    result = exercice.settle_exercice(3, answer="un nouveau", decision="nouveau")
    assert result["status"] == "ok" and result["invoice_id"] == 3
    assert result["message"] == "Facture 3 : exercice 2023 créé et ouvert, la facture y est rattachée."
    assert [call[0] for call in json_calls(experio)] == ["create", "action_open"]
    assert experio["written"] == [({"exercice_id": 61}, {"allowed_company_ids": [5]})]


@pytest.mark.communication
@pytest.mark.parametrize("refused_step", ["create", "action_open", "fill"])
def test_experio_refusing_the_new_exercice_offers_the_open_ones_instead(experio, refused_step):
    experio["refuse"].add(refused_step)
    result = exercice.settle_exercice(3, answer="nouveau", decision="nouveau")
    assert result["status"] == "question" and experio["written"] == []
    assert result["message"] == (
        "Facture 3 : Experio refuse de créer l'exercice 2023 (ValidationError: Chevauchement d'exercices). "
        "La comptabiliser plutôt comme facture antérieure dans un exercice ouvert ? (Exercice 2024, Exercice 2025)"
    )


@pytest.mark.functionality
def test_in_test_mode_no_exercice_is_created(experio):
    experio["dry"] = True
    result = exercice.settle_exercice(3, answer="nouveau", decision="nouveau")
    assert result["status"] == "ok" and result["message"].startswith(TEST_MODE)
    assert "un exercice 2023 (2023-01-01 au 2023-12-31) serait créé" in result["message"]
    assert json_calls(experio) == [] and experio["written"] == []


@pytest.mark.functionality
def test_a_late_invoice_is_booked_by_experios_own_action_in_the_exercice_chosen(experio):
    result = exercice.settle_exercice(3, answer="dans 2025", decision="Exercice 2025")
    assert result["message"] == "Facture 3 : facture antérieure dans l'exercice Exercice 2025."
    context = {"cron_fiscal_entry_id": 2, "allowed_company_ids": [5]}
    assert json_calls(experio) == [("action_activate_old_invoice", [3], context)]


@pytest.mark.communication
def test_a_dossier_without_a_late_invoice_account_is_told_its_current_account_stays(experio):
    experio["company"]["old_invoice_account_id"] = False
    result = exercice.settle_exercice(3, answer="2024", decision="Exercice 2024")
    assert result["status"] == "ok" and "Aucun compte de factures anterieures dans le dossier" in result["message"]
    assert [call[0] for call in json_calls(experio)] == ["action_activate_old_invoice"]


@pytest.mark.functionality
def test_in_test_mode_the_late_invoice_is_only_said(experio):
    experio["dry"] = True
    result = exercice.settle_exercice(3, answer="2025", decision="Exercice 2025")
    assert result["message"].startswith(f"{TEST_MODE}Facture 3 : facture antérieure dans l'exercice Exercice 2025")
    assert json_calls(experio) == []


@pytest.mark.functionality
def test_experio_refusing_the_late_invoice_is_a_tool_error(experio):
    experio["refuse"].add("action_activate_old_invoice")
    with pytest.raises(ToolError, match="Experio a refusé la facture antérieure"):
        exercice.settle_exercice(3, answer="2025", decision="Exercice 2025")


@pytest.mark.communication
def test_without_an_answer_or_with_an_unclear_one_the_client_is_asked(experio):
    asked = exercice.settle_exercice(3)
    assert asked["status"] == "question"
    assert "datée du 2023-06-10, en dehors des exercices ouverts (Exercice 2024, Exercice 2025)" in asked["message"]
    unclear = exercice.settle_exercice(3, answer="je ne sais pas", decision="")
    assert unclear["status"] == "question" and unclear["message"].startswith("Réponse non comprise. Facture 3 :")
    assert json_calls(experio) == [] and experio["written"] == []


@pytest.mark.functionality
def test_an_invoice_already_booked_late_is_left_as_it_is(experio):
    experio["invoice"]["is_old_invoice"] = True
    assert exercice.settle_exercice(3, answer="nouveau", decision="nouveau")["routine"]
    assert json_calls(experio) == [] and experio["written"] == []


@pytest.mark.functionality
def test_an_invoice_without_a_date_is_asked_it_then_the_date_given_is_saved(experio):
    experio["invoice"]["invoice_date"] = False
    asked = exercice.settle_exercice(3, answer="la facture d'hier")
    assert asked["status"] == "question" and "Quelle est sa date (jj/mm/aaaa) ?" in asked["message"]
    assert experio["written"] == []
    result = exercice.settle_exercice(3, answer="le 15/03/2025")  # covered by Exercice 2025: linked to it
    assert experio["written"] == [
        ({"invoice_date": "2025-03-15"}, {"allowed_company_ids": [5]}),
        ({"exercice_id": 2}, {"allowed_company_ids": [5]}),
    ]
    assert result["message"] == "Facture 3 : rattachée à l'exercice Exercice 2025."


@pytest.mark.communication
def test_the_date_answer_is_never_read_as_the_exercice_choice(experio):
    experio["invoice"]["invoice_date"] = False
    result = exercice.settle_exercice(3, answer="15/03/2023", decision="nouveau")
    assert result["status"] == "question" and not result["message"].startswith("Réponse non comprise")
    assert json_calls(experio) == [] and len(experio["written"]) == 1  # the date alone


@pytest.mark.functionality
def test_experio_refusing_the_date_is_a_tool_error(experio):
    experio["invoice"]["invoice_date"], experio["refuse"] = False, {"fill"}
    with pytest.raises(ToolError, match="Experio n'a pas pu enregistrer sa date"):
        exercice.settle_exercice(3, answer="15/03/2025")


@pytest.mark.functionality
def test_an_invoice_already_in_the_covering_exercice_is_not_written(experio):
    experio["invoice"].update(invoice_date="2025-03-15", exercice_id=[2, "Exercice 2025"])
    assert exercice.settle_exercice(3)["routine"] and experio["written"] == []


@pytest.mark.functionality
def test_experio_refusing_the_relink_is_a_tool_error(experio):
    experio["invoice"]["invoice_date"], experio["refuse"] = "2025-03-15", {"fill"}
    with pytest.raises(ToolError, match="Experio n'a pas pu la rattacher à l'exercice"):
        exercice.settle_exercice(3)
