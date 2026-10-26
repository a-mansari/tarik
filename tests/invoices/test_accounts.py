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


DRAFT = {"id": 3, "state": "draft", "type": "purchase", "company_id": [5, "D"], "exercice_id": [2, "2026"]}
DRAFT.update(partner_id=[40, "ALPHA"], account_id=False, invoice_number="F-9", amount_total=1200.0)
CONTEXT = {"allowed_company_ids": [5], "cron_fiscal_entry_id": 2}  # Experio's dossier and exercice of the invoice


@pytest.fixture
def experio(monkeypatch):
    """Experio for assign_account on DRAFT: odoo_call answers ``answers`` by (model, method), every write recorded."""
    state = {"calls": [], "filled": [], "refreshed": [], "answers": {}, "memorized": {"account_id": 61}}
    state["answers"].update(
        {
            ("exp.account", "search_count"): 12,
            ("exp.invoice", "read"): [{"account_id_domain": "[]", "type": "purchase"}],
            ("exp.account", "read"): [{"code": "61250000", "name": "Achats de fournitures"}],
        }
    )

    def odoo_call(model, method, *args, **kw):
        state["calls"].append((model, method, args, kw))
        return state["answers"][(model, method)]

    def fill_invoice(invoice_id, changes, context, save=True):
        if not save:
            return state["memorized"]
        state["filled"].append((invoice_id, changes, context))
        return changes

    monkeypatch.setattr(accounts, "odoo_call", odoo_call)
    monkeypatch.setattr(accounts, "fill_invoice", fill_invoice)
    monkeypatch.setattr(
        accounts, "refresh", lambda invoice_id, context: state["refreshed"].append((invoice_id, context))
    )
    monkeypatch.setattr(accounts, "dry_run", lambda: False)
    monkeypatch.setattr(accounts, "test_mode_note", lambda: "")
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: dict(DRAFT))
    monkeypatch.setattr(accounts, "require_invoice", lambda *a: {})
    monkeypatch.setattr(accounts, "journal_stop", lambda *a: None)
    monkeypatch.setattr(accounts, "check_journal", lambda invoice_id: None)
    monkeypatch.setattr(accounts, "side_of", lambda invoice: "purchase")
    monkeypatch.setattr(accounts, "stored_check", lambda i: (True, {"designation": None}, []))
    return state


@pytest.mark.technical
def test_the_chart_is_read_from_the_invoices_dossier_sorted_by_code(experio):
    experio["answers"][("exp.account", "search_read")] = CHART
    assert accounts.chart(5, ("code", "=like", "6%")) == CHART
    assert experio["calls"] == [
        (
            "exp.account",
            "search_read",
            ([("company_id", "=", 5), ("code", "=like", "6%")],),
            {"fields": ["id", "code", "name"], "order": "code"},
        )
    ]


@pytest.mark.functionality
def test_an_empty_domain_from_experio_leaves_every_account_of_the_sides_classes(experio):
    chart = [{"id": 1, "code": "61110000"}, {"id": 2, "code": "44110000"}, {"id": 3, "code": "21110000"}]
    experio["answers"][("exp.invoice", "read")] = [{"account_id_domain": False, "type": "purchase"}]
    assert [a["id"] for a in accounts.usable(3, chart)] == [1, 3]
    assert [c[:2] for c in experio["calls"]] == [("exp.invoice", "read")]  # no domain: nothing searched
    experio["answers"][("exp.invoice", "read")] = [{"account_id_domain": "[]", "type": "out_refund"}]
    assert accounts.usable(3, chart) == []  # a type outside purchase and sale: no class is warning-free


@pytest.mark.functionality
def test_the_account_is_put_through_experios_form_then_its_refresh_rebuilds_the_lines(experio):
    result = accounts.apply_account(dict(DRAFT), 61, "compte mémorisé")
    assert experio["filled"] == [(3, {"account_id": 61}, CONTEXT)] and experio["refreshed"] == [(3, CONTEXT)]
    assert result == {"status": "ok", "message": "Facture 3 : compte 61250000 Achats de fournitures (compte mémorisé)."}
    assert accounts.account_code_of(61) == "61250000"


@pytest.mark.security
def test_test_mode_says_the_account_would_be_set(experio, monkeypatch):
    monkeypatch.setattr(accounts, "test_mode_note", lambda: accounts.TEST_MODE)
    assert accounts.apply_account(dict(DRAFT), 61, "x")["message"].startswith(accounts.TEST_MODE)


@pytest.mark.communication
@pytest.mark.parametrize("failing", ["fill_invoice", "refresh"])
def test_an_account_experio_refuses_is_said_refused(experio, monkeypatch, failing):
    def refuse(*args, **kwargs):
        raise RuntimeError("Traceback\nValidationError: compte interdit")

    monkeypatch.setattr(accounts, failing, refuse)
    with pytest.raises(accounts.ToolError, match="Facture 3 : Experio a refusé le compte."):
        accounts.apply_account(dict(DRAFT), 61, "x")


@pytest.mark.functionality
def test_a_dossier_with_accounts_gets_no_base_accounts(experio):
    assert accounts._fill_empty_chart(3, 5) is None
    assert experio["calls"] == [("exp.account", "search_count", ([("company_id", "=", 5)],), {})]


@pytest.mark.functionality
def test_an_empty_chart_gets_experios_missing_accounts_wizard_in_the_dossier(experio, monkeypatch):
    experio["answers"].update({("exp.account", "search_count"): 0, ("exp.account.missing.wizard", "create"): 77})
    confirmed = []
    monkeypatch.setattr(accounts, "odoo_json", lambda *args, **kw: confirmed.append((args, kw)))
    assert accounts._fill_empty_chart(3, 5) is None
    assert experio["calls"][1] == (
        "exp.account.missing.wizard",
        "create",
        ({},),
        {"context": {"allowed_company_ids": [5]}},
    )
    assert confirmed == [
        (("exp.account.missing.wizard", "action_confirm", [77]), {"context": {"allowed_company_ids": [5]}})
    ]


@pytest.mark.security
def test_test_mode_creates_no_base_accounts(experio, monkeypatch):
    experio["answers"][("exp.account", "search_count")] = 0
    monkeypatch.setattr(accounts, "dry_run", lambda: True)
    monkeypatch.setattr(accounts, "odoo_json", lambda *a, **k: pytest.fail("test mode confirmed Experio's wizard"))
    result = accounts.assign_account(3)
    assert result["status"] == "ok" and result["message"].startswith(accounts.TEST_MODE)
    assert "les comptes de base seraient créés" in result["message"]
    assert [c[:2] for c in experio["calls"]] == [("exp.account", "search_count")] and experio["filled"] == []


@pytest.mark.functionality
def test_the_memorized_account_is_only_read_from_experio_by_picking_the_tiers_again(experio, monkeypatch):
    seen = []
    monkeypatch.setattr(
        accounts, "fill_invoice", lambda *a, **k: seen.append((a, k)) or {"account_id": 61, "payment_method_id": 2}
    )
    assert accounts._memorized(dict(DRAFT), CONTEXT) == 61
    assert seen == [((3, {"partner_id": 40}, CONTEXT), {"save": False})]  # nothing saved
    assert accounts._memorized({**DRAFT, "partner_id": False}, CONTEXT) is None and len(seen) == 1
    monkeypatch.setattr(accounts, "fill_invoice", lambda *a, **k: {})
    assert accounts._memorized(dict(DRAFT), CONTEXT) is None  # Experio memorized none


@pytest.mark.functionality
def test_the_account_experio_memorized_for_the_tiers_is_applied(experio):
    result = accounts.assign_account(3)
    assert result["status"] == "ok" and "compte mémorisé pour ce tiers" in result["message"]
    assert experio["filled"] == [(3, {"account_id": 61}, CONTEXT)] and experio["refreshed"] == [(3, CONTEXT)]


@pytest.mark.security
def test_a_memorized_account_experio_would_warn_about_is_not_taken(experio):
    experio["answers"][("exp.account", "read")] = [{"code": "44110000", "name": "Fournisseurs"}]
    result = accounts.assign_account(3)
    assert result["status"] == "question" and "la designation est illisible" in result["message"]
    assert experio["filled"] == []


@pytest.mark.communication
def test_experio_failing_while_proposing_an_account_is_said(experio, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("journal manquant")

    monkeypatch.setattr(accounts, "fill_invoice", refuse)
    with pytest.raises(accounts.ToolError, match="Experio n'a pas pu proposer de compte"):
        accounts.assign_account(3)


@pytest.mark.functionality
def test_an_invoice_with_its_account_already_is_left_as_it_is(experio, monkeypatch):
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: {**DRAFT, "account_id": [61, "6125"]})
    result = accounts.assign_account(3)
    assert result["message"] == "Facture 3 : compte déjà renseigné, 61250000 Achats de fournitures."
    assert experio["filled"] == []


@pytest.mark.functionality
def test_a_journal_setting_missing_stops_before_any_account(experio, monkeypatch):
    monkeypatch.setattr(accounts, "journal_stop", lambda *a: {"status": "stop", "message": "journal"})
    assert accounts.assign_account(3) == {"status": "stop", "message": "journal", "invoice_id": 3}
    assert experio["calls"] == []


@pytest.mark.functionality
def test_an_invoice_not_checked_yet_is_refused(experio, monkeypatch):
    experio["memorized"] = {}
    monkeypatch.setattr(accounts, "stored_check", lambda i: None)
    with pytest.raises(accounts.ToolError, match="appelez d'abord check_invoice"):
        accounts.assign_account(3)


@pytest.mark.functionality
@pytest.mark.parametrize("code", [None, "99999999"])
def test_no_account_the_model_finds_in_the_chart_asks_the_client(experio, monkeypatch, code):
    experio["memorized"] = {}
    monkeypatch.setattr(accounts, "stored_check", lambda i: (True, {"designation": "Gasoil"}, []))
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: CHART)
    monkeypatch.setattr(accounts, "ask_model", lambda *a: {"code": code})
    result = accounts.assign_account(3)
    assert result["status"] == "question" and result["message"] == (
        "Facture F-9 (ALPHA, 1200.0 DH TTC) : je n'ai pas pu choisir le compte pour « Gasoil ».\n"
        "Quel compte utiliser (code ou intitulé) ?"
    )
    assert experio["filled"] == []


@pytest.mark.functionality
def test_a_charge_the_model_picks_in_the_chart_is_applied(experio, monkeypatch):
    experio["memorized"] = {}
    monkeypatch.setattr(accounts, "stored_check", lambda i: (True, {"designation": "Téléphone"}, []))
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: CHART)
    monkeypatch.setattr(accounts, "ask_model", lambda *a: {"code": 61455000, "reason": "téléphone"})
    assert accounts.assign_account(3)["status"] == "ok"
    assert experio["filled"] == [(3, {"account_id": 5}, CONTEXT)]


@pytest.mark.communication
def test_the_question_names_the_invoice_by_what_it_has():
    invoice = {"id": 3, "invoice_number": False, "partner_id": False, "amount_total": 0}
    assert accounts.account_question(invoice, None).startswith("Facture 3 : la designation est illisible")


@pytest.mark.security
def test_an_account_the_client_did_not_write_is_asked_about_never_set(experio, monkeypatch):
    monkeypatch.setattr(accounts, "set_account", lambda *a: pytest.fail("set from the agent's word alone"))
    result = accounts.assign_account(3, answer="6145", client_message="Voici la facture")
    assert result["status"] == "question" and "Je n'ai pas retrouvé « 6145 »" in result["message"]
    assert experio["calls"] == []


@pytest.mark.functionality
def test_an_account_the_client_wrote_with_the_invoice_is_set(experio, monkeypatch):
    asked = []
    monkeypatch.setattr(accounts, "set_account", lambda *a: asked.append(a) or {"status": "ok"})
    accounts.assign_account(3, answer="6145", client_message="Compte 6145 svp", confirm=True)
    assert asked == [(3, "6145", True)]


@pytest.mark.functionality
def test_the_account_the_client_answers_is_applied_confirmed_or_asked_again(experio, monkeypatch):
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: CHART)
    monkeypatch.setattr(accounts, "ask_model", lambda *a: {"code": None})
    assert accounts.set_account(3, "61455000")["status"] == "ok"
    assert experio["filled"] == [(3, {"account_id": 5}, CONTEXT)]
    confirm = accounts.set_account(3, "61455000", confirm=True)
    assert confirm["status"] == "question" and confirm["choices"] == {5: "61455000 Frais de téléphone"}
    again = accounts.set_account(3, "Objet : Re: facture\n\ncarburant")
    assert again["status"] == "question" and "« carburant » ne désigne pas un seul compte" in again["message"]
    assert len(experio["filled"]) == 1
    with pytest.raises(accounts.ToolError, match="answer manquant"):
        accounts.set_account(3, "  ")


@pytest.mark.functionality
def test_a_confirmed_account_is_applied_and_a_non_asks_which(experio):
    assert (
        accounts.confirm_account(3, 61, False)["message"]
        == "Facture 3 : quel compte utiliser alors (code ou intitulé) ?"
    )
    assert experio["filled"] == []
    assert "confirmé par le client" in accounts.confirm_account(3, 61, True)["message"]
    assert experio["filled"] == [(3, {"account_id": 61}, CONTEXT)]


@pytest.mark.communication
def test_a_chart_experio_cannot_give_is_said_and_a_long_one_cut_at_100(monkeypatch):
    monkeypatch.setattr(accounts, "require_dossier", lambda dossier_id: "ALPHA")

    def refuse(*args):
        raise RuntimeError("timeout")

    monkeypatch.setattr(accounts, "chart", refuse)
    with pytest.raises(accounts.ToolError, match="Experio n'a pas pu donner le plan comptable."):
        accounts.list_accounts(5)
    many = [{"id": n, "code": f"6{n:07d}", "name": f"Compte {n}"} for n in range(120)]
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: many)
    listed = accounts.list_accounts(5)
    assert listed.startswith("120 compte(s) du dossier ALPHA (les 100 premiers") and listed.count("\n") == 100


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("words", "matching", "chosen"),
    [
        ("gasoil", [], True),  # no account called so: the client describes the invoice
        ("transport", [{"id": 7, "code": "61425000", "name": "Transports"}], False),  # names accounts: theirs to pick
    ],
)
def test_words_naming_no_account_guide_the_choice_and_are_said(experio, monkeypatch, words, matching, chosen):
    monkeypatch.setattr(accounts, "set_account", lambda *a: {"status": "question", "message": "lequel ?"})
    monkeypatch.setattr(accounts, "searched", lambda found, search: matching)
    monkeypatch.setattr(accounts, "chart", lambda company_id, *domain: [])
    monkeypatch.setattr(accounts, "stored_check", lambda i: (True, {"designation": "Gasoil 40 L"}, []))
    monkeypatch.setattr(
        accounts,
        "require_invoice",
        lambda invoice_id, fields: {"company_id": [857, "D"], **{f: False for f in fields if f != "company_id"}},
    )
    picked = []

    def choice(invoice, kind, designation):
        picked.append(designation)
        return {"id": 9, "code": "61241000", "name": "Achats de carburants"}, "du gasoil pour les véhicules"

    monkeypatch.setattr(accounts, "_model_choice", choice)
    monkeypatch.setattr(accounts, "_memorized", lambda *a: pytest.fail("the client's words come before the memory"))
    result = accounts.assign_account(3, answer=words, client_message=f"{words} TARIK TEST")
    if not chosen:
        assert result["status"] == "question" and not picked
        return
    assert picked and f"le client précise : « {words} »" in picked[0]
    assert result["status"] == "ok" and f"« {words} » ne désigne aucun compte du dossier" in result["message"]
