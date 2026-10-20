"""The invoice's tiers: names compared, the client's choice among those proposed (fictional tiers)."""

import pytest

from tarik.core.config import TIERS_MIN_SIMILARITY
from tarik.core.words import similarity

TIERS = [
    {"id": 7, "name": "ATLAS DE ROULEMENT - 12345678"},
    {"id": 8, "name": "ATLAS TRANSPORT"},
    {"id": 9, "name": "NOVA TELECOM"},
    {"id": 10, "name": "NOVA TELECOM", "ice": "001234567000089"},
]


@pytest.mark.functionality
def test_names_compared():
    assert (
        similarity("GAMMA DISTRIBUTION SARL", "DELTA FAIR DISTRIBUTION") < TIERS_MIN_SIMILARITY
    )  # shared generic words only
    assert similarity("atlas roulement", "ATLAS DE ROULEMENT") >= TIERS_MIN_SIMILARITY
    assert (
        similarity("Nova Telecom SA", "NOVA TELECOM")
        >= TIERS_MIN_SIMILARITY
        > similarity("Nova Telecom SA", "ORBIT TELECOM")
    )


CHOICES = {"7": "ATLAS DE ROULEMENT", "8": "ATLAS TRANSPORT"}  # as record_step kept them: keys are text


@pytest.fixture
def chosen(monkeypatch):
    """choose_tiers on invoice 4 of dossier 778 (TIERS): the tiers it links, else its question."""
    from tarik.invoices import tiers

    invoice = {"id": 4, "company_id": [778, "D"], "type": "purchase", "partner_id": False}
    monkeypatch.setattr(tiers, "require_draft_invoice", lambda invoice_id, fields, what, read: invoice)
    monkeypatch.setattr(tiers, "require_invoice", lambda invoice_id, fields: invoice)
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [{"ice": "", **p} for p in TIERS])
    monkeypatch.setattr(tiers, "side_of", lambda invoice: "purchase")
    monkeypatch.setattr(tiers, "link", lambda invoice, partner_id, kind: {"status": "ok", "linked": partner_id})
    monkeypatch.setattr(tiers, "create_tiers", lambda invoice, name, kind: {"status": "ok", "created": name})

    def choose(decision, answer):
        result = tiers.choose_tiers(4, decision, answer, CHOICES)
        return result.get("linked") or result.get("created") or result["message"]

    return choose


@pytest.mark.security
def test_the_agent_chooses_among_the_tiers_proposed_only(chosen):
    assert chosen("7", "le premier") == 7 and chosen(" 8 ", "ATLAS TRANSPORT") == 8
    assert "ne designe pas un seul tiers" in chosen("9", "oui")  # an id the question did not propose
    assert "ne designe pas un seul tiers" in chosen("", "BETA SARL, TVA 20 %")  # a number in the answer is no id


@pytest.mark.functionality
def test_a_proposed_tiers_is_named_by_its_id_or_its_name():
    from tarik.core.conversation import chosen_among

    assert chosen_among("7", CHOICES) == chosen_among("id 7", CHOICES) == chosen_among("n° 7", CHOICES) == 7
    assert chosen_among("atlas transport", CHOICES) == 8  # its name as proposed
    assert chosen_among("ATLAS", CHOICES) is None and chosen_among("9", CHOICES) is None
    assert chosen_among("7", None) is None  # nothing proposed


@pytest.mark.functionality
def test_another_tiers_the_client_names_is_searched_or_linked_by_its_ice(chosen):
    assert chosen("001234567000089", "non, celui avec l'ICE 001234567000089") == 10  # an ICE names one tiers
    proposed = chosen("Nova Telecom", "non, c'est plutôt Nova Telecom")
    assert "« Nova Telecom »" in proposed and "(id 9" in proposed and "(id 10" in proposed  # proposed, not picked
    assert chosen("Beta Services", "c'est Beta Services") == "Beta Services"  # nothing like it: created as written
    assert "ne designe pas" in chosen("009999999000011", "ICE 009999999000011")  # an unknown ICE creates nothing


@pytest.mark.security
@pytest.mark.parametrize(
    "autofill, said", [(True, "serait cree par Experio"), (False, "le cabinet les attribue lui-meme")]
)
def test_test_mode_creates_no_wizard_in_experio(monkeypatch, autofill, said):
    from tarik.invoices import tiers

    def odoo_call(model, method, *args, **kw):
        assert method == "read", f"test mode wrote to Experio: {model}.{method}"
        return (
            [{"name": "ATLAS", "third_party_supplier_account": False}]
            if model == "exp.partner"
            else [{"third_party_supplier_autofill": autofill}]
        )

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    monkeypatch.setattr(tiers, "dry_run", lambda: True)
    assert said in tiers.ensure_tiers_account(1, 7, "purchase", {"allowed_company_ids": [778]})


@pytest.fixture
def scanned(monkeypatch):
    """link_tiers on a purchase whose scan names a supplier Experio did not find: what Experio is asked."""
    from tarik.invoices import tiers

    invoice = {
        "id": 4,
        "company_id": [778, "D"],
        "type": "purchase",
        "partner_id": False,
        "exercice_id": False,
        "detected_supplier_id": False,
        "supplier_name": "BETA SERVICES",
        "detected_supplier_ice": "001234567000090",
        "detected_supplier_if": "",
    }
    calls = []
    monkeypatch.setattr(tiers, "require_draft_invoice", lambda invoice_id, fields, what, read: invoice)
    monkeypatch.setattr(tiers, "require_invoice", lambda invoice_id, fields: invoice)
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [{"ice": "", **p} for p in TIERS])
    monkeypatch.setattr(tiers, "side_of", lambda invoice: "purchase")

    def link(invoice, partner_id, kind):
        return {"status": "ok", "message": f"fournisseur BETA SERVICES, id {partner_id}"}

    def odoo_call(model, method, values, **kw):
        calls.append((model, method, values))
        return 55

    monkeypatch.setattr(tiers, "link", link)
    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    monkeypatch.setattr(tiers, "dry_run", lambda: False)
    monkeypatch.setattr(tiers, "test_mode_note", lambda: "")
    return invoice, calls


@pytest.mark.functionality
def test_a_tiers_nothing_resembles_is_created_from_the_scan(scanned):
    from tarik.invoices import tiers

    invoice, calls = scanned
    result = tiers.link_tiers(4)
    assert calls == [
        (
            "exp.partner",
            "create",
            {
                "name": "BETA SERVICES",
                "type": "company",
                "company_id": 778,
                "is_supplier": True,
                "ice": "001234567000090",
            },
        )
    ]
    assert result["message"] == "fournisseur BETA SERVICES cree, id 55"
    invoice["supplier_name"], calls[:] = "Atlas Transports", []
    assert tiers.link_tiers(4)["status"] == "question" and not calls  # close names: asked, not created


@pytest.mark.security
def test_test_mode_creates_no_tiers(scanned, monkeypatch):
    from tarik.invoices import tiers

    invoice, calls = scanned
    monkeypatch.setattr(tiers, "dry_run", lambda: True)
    assert "serait cree" in tiers.link_tiers(4)["message"] and not calls
