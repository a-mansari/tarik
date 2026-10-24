"""The client's words -> one account (fictional chart, same shapes as a real one)."""

import pytest

from engine.tools.invoices import accounts
from engine.tools.invoices.accounts import account_meant, resolve_account

CHART = [
    {"id": 1, "code": "61110000", "name": "Achats de marchandises"},
    {"id": 2, "code": "61120000", "name": "Achats de marchandises"},
    {"id": 3, "code": "61190000", "name": "Rabais, remises et ristournes obtenus sur achats de marchandises"},
    {"id": 4, "code": "61251000", "name": "Achats de fournitures non stockables"},
    {"id": 5, "code": "61455000", "name": "Frais de téléphone"},
]


@pytest.mark.functionality
def test_one_account_or_a_question():
    assert resolve_account(CHART, "achats de marchandises")["id"] == 1  # same label twice: lowest code
    assert resolve_account(CHART, "6111")["id"] == 1 and resolve_account(CHART, "61455000")["id"] == 5
    assert resolve_account(CHART, "téléphone")["id"] == 5
    assert resolve_account(CHART, "marchandises") is None  # three different labels: ask
    assert resolve_account(CHART, "611") is None and resolve_account(CHART, "carburant") is None
    tel = [
        {"id": 6, "code": "61450000", "name": "Frais postaux et telecommunications"},
        {"id": 7, "code": "61451000", "name": "Frais postaux"},
        {"id": 8, "code": "61455000", "name": "Frais de telephone"},
    ]
    assert (
        resolve_account(tel, "6145")["id"] == 6 and resolve_account(tel, "61455")["id"] == 8
    )  # a short code: padded with zeros


@pytest.mark.security
@pytest.mark.parametrize(
    "read, expected",
    [("61455000", 5), (61455000, 5), ("6145", 5), ("611", None), ("99999999", None), ("frais", None), (None, None)],
)
def test_a_sentence_is_read_by_the_model_but_only_the_charts_accounts_count(monkeypatch, read, expected):
    monkeypatch.setattr(accounts, "ask_model", lambda *args: {"code": read})
    found = account_meant(CHART, "Pour MEDI TELECOM, utilisez le compte 6145.")
    assert (found or {}).get("id") == expected


@pytest.mark.functionality
def test_a_code_start_the_client_wrote_is_theirs_not_the_models(monkeypatch):
    travel = [
        {"id": 9, "code": "61431000", "name": "Voyages et déplacements"},
        {"id": 10, "code": "61436000", "name": "Réceptions"},
    ]
    monkeypatch.setattr(accounts, "ask_model", lambda *args: {"code": "61431000"})
    assert account_meant(travel, "6143") is None  # two accounts start with it: asked again
    assert account_meant(travel, "Mettez 6143, 2 factures") is None
    assert account_meant(travel, "61431 voyages")["id"] == 9
    assert account_meant(travel, "Voyages et déplacements")["id"] == 9  # no code written: the model's


@pytest.mark.functionality
def test_the_agent_finds_accounts_by_code_or_misspelled_words(monkeypatch):
    chart = [
        {"id": 9, "code": "61421000", "name": "Transports sur achats"},
        {"id": 10, "code": "61436000", "name": "Frais d'hôtellerie et de restauration"},
        {"id": 11, "code": "35000000", "name": "Titres et valeurs de placement"},
        *CHART,
    ]
    monkeypatch.setattr(accounts, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: chart)
    assert (
        accounts.list_accounts(5, "hotelrie")
        == "1 compte(s) du dossier ALPHA :\n61436000 Frais d'hôtellerie et de restauration"
    )
    assert "61421000" in accounts.list_accounts(5, "transport")
    assert [a["id"] for a in accounts.searched(chart, "6111")] == [1]
    assert [a["id"] for a in accounts.searched(chart, "achats de marchandises")] == [1, 2, 3]
    assert accounts.list_accounts(5, "carburant") == "Aucun compte du dossier ALPHA ne correspond a « carburant »."
    assert accounts.searched(chart, "deplacement") == []  # not "placement"
    assert accounts.list_accounts(5).startswith("8 compte(s)")


@pytest.mark.functionality
def test_a_side_keeps_its_class_of_accounts_only_and_any_other_is_refused(monkeypatch):
    asked = []
    monkeypatch.setattr(accounts, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: asked.append(domain) or CHART)
    accounts.list_accounts(5, "achats", side="achat")
    accounts.list_accounts(5, side="vente")
    accounts.list_accounts(5)
    assert asked == [(("code", "=like", "6%"),), (("code", "=like", "7%"),), ()]
    with pytest.raises(accounts.ToolError, match="inconnu"):
        accounts.list_accounts(5, side="charges")


@pytest.mark.security
def test_only_accounts_experios_field_offers_and_posts_without_warning_are_usable(monkeypatch):
    """Experio's own Account domain (account_id_domain: no reconcilable nor tiers account) and the classes it posts
    without a warning (2 and 6 for a purchase, 7 for a sale)."""
    chart = [
        {"id": 1, "code": "21110000", "name": "Frais de constitution"},
        {"id": 2, "code": "44110000", "name": "Fournisseurs"},  # a tiers account: not in Experio's domain
        {"id": 3, "code": "61110000", "name": "Achats de marchandises"},
        {"id": 4, "code": "71110000", "name": "Ventes de marchandises"},
        {"id": 5, "code": "34550000", "name": "Etat TVA recuperable"},  # in the domain, but class 3: warned
    ]
    invoice = {"type": "purchase", "account_id_domain": "[('company_id', '=', 5), ('reconcile', '=', False)]"}
    searched = []

    def odoo_call(model, method, *args, **kw):
        if model == "exp.invoice":
            return [dict(invoice)]
        searched.append(args[0])
        return [1, 3, 4, 5]  # what Experio's domain finds: not the tiers account

    monkeypatch.setattr(accounts, "odoo_call", odoo_call)
    assert [a["id"] for a in accounts.usable(7, chart)] == [1, 3]
    assert searched == [[("company_id", "=", 5), ("reconcile", "=", False)]]  # Experio's domain as it gives it
    invoice["type"] = "sale"
    assert [a["id"] for a in accounts.usable(7, chart)] == [4]


@pytest.mark.security
def test_an_immobilisation_the_model_picks_is_confirmed_by_the_client_first(monkeypatch):
    """No Experio rule tells a fixed asset from a charge: a class 2 account the model picks is asked, never applied."""
    invoice = {"id": 3, "state": "draft", "type": "purchase", "company_id": [5, "D"], "exercice_id": False}
    invoice.update(partner_id=False, account_id=False, invoice_number="F-9", amount_total=12000.0)
    chart = [
        {"id": 21, "code": "23550000", "name": "Matériel informatique"},
        {"id": 61, "code": "61250000", "name": "Achats"},
    ]
    searched = []
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: invoice)
    monkeypatch.setattr(accounts, "journal_stop", lambda *a: None)
    monkeypatch.setattr(accounts, "require_invoice", lambda *a: {})
    monkeypatch.setattr(accounts, "check_journal", lambda invoice_id: None)
    monkeypatch.setattr(accounts, "side_of", lambda invoice: "purchase")
    monkeypatch.setattr(accounts, "_fill_empty_chart", lambda *a: None)
    monkeypatch.setattr(accounts, "_memorized", lambda *a: None)
    monkeypatch.setattr(accounts, "invoice_context", lambda invoice: {})
    monkeypatch.setattr(accounts, "stored_check", lambda i: (True, {"designation": "Ordinateur portable Dell"}, []))
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: searched.append(domain) or chart)
    monkeypatch.setattr(accounts, "ask_model", lambda *a: {"code": "23550000", "reason": "un ordinateur"})
    monkeypatch.setattr(accounts, "apply_account", lambda *a: pytest.fail("never applied before the client confirms"))
    asked = accounts.assign_account(3)
    assert asked["status"] == "question" and asked["choices"] == {21: "23550000 Matériel informatique"}
    assert "ressemble à une immobilisation" in asked["message"] and asked["message"].endswith("oui / non")
    assert searched[0] == ("|", ("code", "=like", "6%"), ("code", "=like", "2%"))  # classes 6 and 2 offered
