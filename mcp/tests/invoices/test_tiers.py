"""The invoice's tiers: names compared, the client's free-text answer (fictional tiers)."""

import pytest

from tarik.core.config import TIERS_MIN_SIMILARITY
from tarik.core.text import similarity
from tarik.invoices.tiers import pick_tiers

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


@pytest.mark.functionality
def test_the_client_answer_picks_one_tiers_or_none():
    assert pick_tiers(TIERS, "Oui, c'est bien ATLAS DE ROULEMENT (id 7).")["id"] == 7
    assert pick_tiers(TIERS, "c'est ATLAS DE ROULEMENT")["id"] == 7
    assert pick_tiers(TIERS, "oui", proposed_ids=[9])["id"] == 9
    assert pick_tiers(TIERS, "oui", proposed_ids=[7, 9]) is None  # two proposals: a yes doesn't choose
    assert pick_tiers(TIERS, "atlas") is None  # two tiers start with it
    assert pick_tiers(TIERS, "nova telecom sa") is None  # the same name twice: ask
    assert pick_tiers(TIERS, "celui avec l'ICE 001234567000089")["id"] == 10


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
