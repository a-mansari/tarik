"""The invoice's tiers: names compared, the client's choice among those proposed (fictional tiers)."""

import pytest

from engine.tools.core.config import TIERS_MIN_SIMILARITY
from engine.tools.core.words import similarity
from engine.tools.invoices.tiers import flagged_tier

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
    from engine.tools.invoices import tiers

    invoice = {"id": 4, "company_id": [778, "D"], "type": "purchase", "partner_id": False}
    monkeypatch.setattr(tiers, "require_draft_invoice", lambda invoice_id, fields, what, read: invoice)
    monkeypatch.setattr(tiers, "require_invoice", lambda invoice_id, fields: invoice)
    monkeypatch.setattr(tiers, "flagged_tier", lambda invoice_id: None)
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
    assert "ne désigne pas un seul tiers" in chosen("9", "oui")  # an id the question did not propose
    assert "ne désigne pas un seul tiers" in chosen("", "BETA SARL, TVA 20 %")  # a number in the answer is no id


@pytest.mark.functionality
def test_a_proposed_tiers_is_named_by_its_id_or_its_name():
    from engine.tools.core.conversation import chosen_among

    assert chosen_among("7", CHOICES) == chosen_among("id 7", CHOICES) == chosen_among("n° 7", CHOICES) == 7
    assert chosen_among("atlas transport", CHOICES) == 8  # its name as proposed
    assert chosen_among("ATLAS", CHOICES) is None and chosen_among("9", CHOICES) is None
    assert chosen_among("7", None) is None  # nothing proposed


@pytest.mark.functionality
def test_another_tiers_the_client_names_is_searched_or_linked_by_its_ice(chosen):
    assert chosen("001234567000089", "non, celui avec l'ICE 001234567000089") == 10  # an ICE names one tiers
    proposed = chosen("Nova Telecom", "non, c'est plutôt Nova Telecom")
    assert "« Nova Telecom »" in proposed and "(id `9`" in proposed and "(id `10`" in proposed  # proposed, not picked
    assert chosen("Beta Services", "c'est Beta Services") == "Beta Services"  # nothing like it: created as written
    assert "ne désigne pas" in chosen("009999999000011", "ICE 009999999000011")  # an unknown ICE creates nothing


@pytest.mark.security
@pytest.mark.parametrize(
    "autofill, said", [(True, "serait créé par Experio"), (False, "le cabinet les attribue lui-même")]
)
def test_test_mode_creates_no_wizard_in_experio(monkeypatch, autofill, said):
    from engine.tools.invoices import tiers

    def odoo_call(model, method, *args, **kw):
        assert method in ("read", "search_read"), f"test mode wrote to Experio: {model}.{method}"
        if method == "search_read":  # the dossier's tiers accounts: none to follow
            return []
        if model == "res.company" and "third_party_supplier_account" in kw["fields"]:
            return [{"third_party_supplier_account": "4411"}]
        return (
            [{"name": "ATLAS", "ice": False, "vat": False, "third_party_supplier_account": False}]
            if model == "exp.partner"
            else [{"third_party_supplier_autofill": autofill}]
        )

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    monkeypatch.setattr(tiers, "dry_run", lambda: True)
    assert said in tiers.ensure_tiers_account(1, 7, "purchase", {"allowed_company_ids": [778]})


@pytest.fixture
def scanned(monkeypatch):
    """link_tiers on a purchase whose scan names a supplier Experio did not find: what Experio is asked."""
    from engine.tools.invoices import tiers

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
    monkeypatch.setattr(tiers, "flagged_tier", lambda invoice_id: None)
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [{"ice": "", **p} for p in TIERS])
    monkeypatch.setattr(tiers, "side_of", lambda invoice: "purchase")

    def link(invoice, partner_id, kind):
        return {"status": "ok", "message": f"fournisseur `BETA SERVICES`, id {partner_id}"}

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
    from engine.tools.invoices import tiers

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
    assert result["message"] == "fournisseur `BETA SERVICES` créé, id 55"
    invoice["supplier_name"], calls[:] = "Atlas Transports", []
    assert tiers.link_tiers(4)["status"] == "question" and not calls  # close names: asked, not created


@pytest.mark.functionality
def test_a_tiers_the_registry_does_not_know_is_asked_about_then_created_unchecked_on_the_client_s_word(
    scanned, monkeypatch
):
    import xmlrpc.client

    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice.update({"supplier_name": "mIDAS", "detected_supplier_ice": "003401052000041"})
    invoice["detected_supplier_if"] = "60106127"
    registry = "Aucune donnée n'a pu être récupérée pour MIDAS avec IF 60106127 et ICE 003401052000041"

    def odoo_call(model, method, values, **kw):
        calls.append((model, method, values))
        if not values.get("ignore_ice_if_verification"):
            raise xmlrpc.client.Fault(2, registry)
        return 56

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    monkeypatch.setattr(tiers, "chosen_among", lambda decision, choices: None)
    asked = tiers.link_tiers(4)
    assert asked["status"] == "question" and "« MIDAS »" in asked["message"]  # its scrambled case put right
    assert "ICE 003401052000041, IF 60106127" in asked["message"] and "sans vérification" in asked["message"]
    assert calls[0][2]["name"] == "MIDAS"
    calls.clear()
    assert tiers.choose_tiers(4, "sans vérification", "Oui, créez-le sans vérification.")["status"] == "ok"
    assert calls[0][2]["ignore_ice_if_verification"] is True and calls[0][2]["name"] == "MIDAS"


@pytest.mark.communication
def test_why_experio_refused_a_tiers_is_said(scanned, monkeypatch):
    import xmlrpc.client

    from engine.tools.core.app import ToolError
    from engine.tools.invoices import tiers

    def odoo_call(model, method, values, **kw):
        raise xmlrpc.client.Fault(2, "Le nom du tiers est obligatoire")

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    with pytest.raises(ToolError, match=r"refusé le tiers `BETA SERVICES` \(.*Le nom du tiers est obligatoire"):
        tiers.link_tiers(4)


@pytest.mark.security
def test_test_mode_creates_no_tiers(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    monkeypatch.setattr(tiers, "dry_run", lambda: True)
    assert "serait créé" in tiers.link_tiers(4)["message"] and not calls


@pytest.mark.functionality
def test_a_supplier_only_the_check_read_proposes_the_closest_tiers_never_creates(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["supplier_name"] = False  # Experio's reading left it empty (invoice 272383)
    read = {"tier": "ORDRE DES EXPERTS COMPTABLES - CONSEIL REGIONAL DE RABAT", "ice": "001818244000082"}
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: (True, read, []))
    monkeypatch.setattr(
        tiers,
        "dossier_tiers",
        lambda company_id: [
            {"id": 1, "name": "FOURNISSEUR ALPHA", "ice": ""},
            {"id": 2, "name": "STE L ORDRE DES EXPERTS COMPTABLES", "ice": ""},
            {"id": 3, "name": "CRRSK", "ice": "001818244000082"},
        ],
    )
    result = tiers.link_tiers(4)
    assert result["status"] == "question" and "« ORDRE DES EXPERTS COMPTABLES" in result["message"]
    assert list(result["choices"]) == [3, 2]  # its ICE first, then the close name; ALPHA too far
    assert calls == []


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("tiers_found", "linked"),
    [
        ([{"id": 7, "name": "Beta Services."}], 7),  # the same name, case and punctuation aside: linked
        ([{"id": 7, "name": "BETA SERVICES"}, {"id": 8, "name": "Beta-Services"}], None),  # two of that name: asked
        ([{"id": 7, "name": "BETA SERVICE"}], None),  # close is not the same: asked
    ],
)
def test_only_a_tiers_of_the_very_same_name_is_linked_and_the_client_told(scanned, monkeypatch, tiers_found, linked):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    found = [*tiers_found, {"id": 9, "name": "BETA SERVICES SARL"}]
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [{"ice": "", **p} for p in found])
    result = tiers.link_tiers(4)
    if linked is None:
        assert result["status"] == "question"
    else:
        assert result["status"] == "ok" and f"(id `{linked}`)" in result["message"]
        assert "même nom" in result["message"] and "BETA SERVICES SARL" in result["message"]  # the lookalike named
    assert calls == []


@pytest.mark.security
def test_a_tiers_already_set_is_kept_over_the_one_detected(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice.update(partner_id=[5, "SET BY HAND"], detected_supplier_id=[6, "DETECTED"])
    linked = []
    monkeypatch.setattr(tiers, "link", lambda invoice, partner_id, kind: linked.append(partner_id) or {"status": "ok"})
    tiers.link_tiers(4)
    assert linked == [5] and calls == []


CONTEXT = {"allowed_company_ids": [778]}  # Experio's dossier of invoice 4, no exercice


@pytest.fixture
def linking(monkeypatch):
    """link on purchase 4 of dossier 778: odoo_call answers ``answers`` by (model, method), Experio's writes kept."""
    from engine.tools.invoices import tiers

    invoice = {"id": 4, "company_id": [778, "D"], "exercice_id": False, "type": "purchase", "partner_id": False}
    state = {"invoice": invoice, "calls": [], "filled": [], "confirmed": []}
    state["answers"] = {
        ("exp.partner", "read"): lambda fields: [
            {"name": "ATLAS", "company_id": [778, "D"], "active": True, "third_party_supplier_account": False}
            | {"third_party_customer_account": False, "ice": False, "vat": False}
        ],
        ("res.company", "read"): lambda fields: [
            {"third_party_supplier_account": "4411", "third_party_customer_account": "3421"}
        ],
        ("exp.partner", "search_read"): lambda fields: [],
        ("exp.partner", "search_count"): lambda fields: 0,
        ("exp.invoice.partner.account.wizard", "write"): lambda fields: True,
        ("exp.invoice.partner.account.wizard", "create"): lambda fields: 31,
        ("exp.invoice.partner.account.wizard", "read"): lambda fields: [{"third_party_account": "44110123"}],
    }

    def odoo_call(model, method, *args, **kw):
        state["calls"].append((model, method, args, kw))
        return state["answers"][(model, method)](kw.get("fields"))

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    monkeypatch.setattr(tiers, "fill_invoice", lambda *a: state["filled"].append(a))
    monkeypatch.setattr(tiers, "odoo_json", lambda *a, **k: state["confirmed"].append((a, k)))
    monkeypatch.setattr(tiers, "dry_run", lambda: False)
    monkeypatch.setattr(tiers, "test_mode_note", lambda: "")
    return state


@pytest.mark.functionality
def test_a_tiers_is_put_through_experios_form_and_gets_the_compte_tiers_its_wizard_proposes(linking):
    from engine.tools.invoices import tiers

    result = tiers.link(linking["invoice"], 7, "purchase")
    assert result == {"status": "ok", "message": "Facture 4 : fournisseur `ATLAS`, compte tiers `44110123` créé."}
    assert linking["filled"] == [(4, {"partner_id": 7}, CONTEXT)]
    assert linking["calls"][0] == (
        "exp.partner",
        "read",
        ([7],),
        {"fields": ["name", "company_id", "active"], "context": CONTEXT},
    )
    wizard = "exp.invoice.partner.account.wizard"
    assert linking["calls"][2] == (wizard, "create", ({"invoice_id": 4},), {"context": CONTEXT})
    assert linking["confirmed"] == [((wizard, "action_confirm", [31]), {"context": CONTEXT})]


@pytest.mark.functionality
def test_a_tiers_already_on_the_invoice_with_its_compte_tiers_writes_nothing(linking):
    from engine.tools.invoices import tiers

    linking["invoice"]["partner_id"] = [7, "ATLAS"]
    linking["answers"][("exp.partner", "read")] = lambda fields: [
        {"name": "ATLAS", "company_id": [778, "D"], "third_party_supplier_account": "44110007"}
    ]
    assert tiers.link(linking["invoice"], 7, "purchase")["message"] == (
        "Facture 4 : fournisseur `ATLAS`, compte tiers `44110007`."
    )
    assert linking["filled"] == [] and linking["confirmed"] == [] and len(linking["calls"]) == 2


@pytest.mark.functionality
def test_a_tiers_of_a_dossier_without_a_series_is_told_the_cabinet_assigns_it(linking):
    from engine.tools.invoices import tiers

    linking["answers"][("exp.invoice.partner.account.wizard", "read")] = lambda fields: [{"third_party_account": False}]
    linking["invoice"]["type"] = "sale"
    result = tiers.link(linking["invoice"], 7, "sale")
    assert "client `ATLAS`, pas de Compte Tiers client : dans ce dossier le cabinet les attribue" in result["message"]
    assert linking["confirmed"] == []  # no code proposed, none to follow: the wizard is never confirmed


def numbered(linking, codes, prefix="4411"):
    """A dossier with numbering off whose supplier accounts are ``codes``: link a tiers without one."""
    linking["answers"][("exp.invoice.partner.account.wizard", "read")] = lambda fields: [{"third_party_account": False}]
    linking["answers"][("res.company", "read")] = lambda fields: [{"third_party_supplier_account": prefix}]
    linking["answers"][("exp.partner", "search_read")] = lambda fields: [
        {"third_party_supplier_account": c} for c in codes
    ]
    return linking


@pytest.mark.functionality
def test_a_tiers_without_account_gets_the_next_code_of_the_dossiers_own_series(linking):
    """A FEC's chart: 9-character codes after 4411, whatever Experio's stored length (8) and counter (1) say."""
    from engine.tools.invoices import tiers

    numbered(linking, ["441125726", "441125725", "441100030"])
    result = tiers.link(linking["invoice"], 7, "purchase")
    assert (
        "compte tiers `441125727` attribué par Tarik, dans la suite de la numérotation du dossier" in result["message"]
    )
    wizard = "exp.invoice.partner.account.wizard"
    assert (wizard, "write", ([31], {"third_party_account": "441125727"}), {"context": CONTEXT}) in linking["calls"]
    assert linking["confirmed"] == [((wizard, "action_confirm", [31]), {"context": CONTEXT})]


@pytest.mark.functionality
def test_the_series_skips_a_code_a_tiers_holds_and_follows_a_short_series(linking):
    from engine.tools.invoices import tiers

    numbered(linking, ["44110033", "44110034"])
    taken = iter([1, 0])  # 44110035 is held by an archived tiers
    linking["answers"][("exp.partner", "search_count")] = lambda fields: next(taken)
    assert "`44110036`" in tiers.link(linking["invoice"], 7, "purchase")["message"]


@pytest.mark.functionality
def test_no_series_means_no_invented_code(linking):
    from engine.tools.invoices import tiers

    numbered(linking, [])  # the dossier has no tiers account yet
    assert "le cabinet les attribue" in tiers.link(linking["invoice"], 7, "purchase")["message"]
    numbered(linking, ["4411ABC001"])  # a lettered series: no number to follow
    assert "le cabinet les attribue" in tiers.link(linking["invoice"], 7, "purchase")["message"]
    assert linking["confirmed"] == []


@pytest.mark.functionality
def test_a_foreign_tiers_is_left_to_the_cabinet(linking):
    from engine.tools.invoices import tiers

    numbered(linking, ["441125726"])
    linking["answers"][("exp.partner", "read")] = lambda fields: [
        {"name": "ACME", "company_id": [778, "D"], "active": True, "ice": "2222", "vat": False}
        | {"third_party_supplier_account": False, "third_party_customer_account": False}
    ]
    assert "le cabinet les attribue" in tiers.link(linking["invoice"], 7, "purchase")["message"]


@pytest.mark.security
@pytest.mark.parametrize("found", [[], [{"name": "OTHER", "company_id": [999, "X"], "active": True}]])
def test_a_tiers_of_another_dossier_or_none_is_never_linked(linking, found):
    from engine.tools.core.app import ToolError
    from engine.tools.invoices import tiers

    linking["answers"][("exp.partner", "read")] = lambda fields: found
    with pytest.raises(ToolError, match="n'appartient pas au dossier de la facture 4"):
        tiers.link(linking["invoice"], 7, "purchase")
    assert linking["filled"] == []


@pytest.mark.communication
def test_why_experio_refused_to_link_a_tiers_is_said(linking, monkeypatch):
    from engine.tools.core.app import ToolError
    from engine.tools.invoices import tiers

    def refuse(*args):
        raise RuntimeError("Traceback\nValidationError: tiers archivé")

    monkeypatch.setattr(tiers, "fill_invoice", refuse)
    with pytest.raises(ToolError, match=r"Facture 4 : Experio a refusé le tiers `ATLAS` \(.*tiers archivé\)"):
        tiers.link(linking["invoice"], 7, "purchase")
    assert linking["confirmed"] == []


@pytest.mark.technical
def test_the_dossiers_tiers_are_read_inside_the_dossier(linking):
    from engine.tools.invoices import tiers

    linking["answers"][("exp.partner", "search_read")] = lambda fields: [{"id": 7, "name": "ATLAS", "ice": False}]
    assert tiers.dossier_tiers(778) == [{"id": 7, "name": "ATLAS", "ice": False}]
    assert linking["calls"] == [
        (
            "exp.partner",
            "search_read",
            ([("company_id", "=", 778)],),
            {"fields": ["id", "name", "ice"], "context": CONTEXT},
        )
    ]


@pytest.mark.functionality
def test_the_tiers_experio_detected_is_linked(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["detected_supplier_id"] = [6, "DETECTED"]
    linked = []
    monkeypatch.setattr(tiers, "link", lambda invoice, partner_id, kind: linked.append(partner_id) or {"status": "ok"})
    tiers.link_tiers(4)
    assert linked == [6] and calls == []


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("found", "status"),
    [
        ([{"id": 3, "name": "ADMINISTRATION DES DOUANES"}], "ok"),
        ([{"id": 3, "name": "Douane Casa"}, {"id": 5, "name": "ADII Tanger"}], "question"),
    ],
)
def test_a_customs_invoice_gets_the_dossiers_one_customs_tiers_or_asks_among_several(
    scanned, monkeypatch, found, status
):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["import_invoice_type"] = "customs"
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [*found, {"id": 9, "name": "ATLAS"}])
    monkeypatch.setattr(
        tiers, "link", lambda invoice, partner_id, kind: {"status": "ok", "message": f"fournisseur id {partner_id}"}
    )
    result = tiers.link_tiers(4)
    assert result["status"] == status
    if status == "ok":
        assert result["message"] == "administration des douanes id 3"
    else:
        assert (
            result["choices"] == {3: "Douane Casa", 5: "ADII Tanger"}
            and "plusieurs tiers des douanes" in result["message"]
        )
    assert calls == []


@pytest.mark.functionality
def test_a_customs_invoice_without_customs_tiers_goes_the_usual_way(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["import_invoice_type"] = "customs"
    assert tiers.link_tiers(4)["message"] == "fournisseur `BETA SERVICES` créé, id 55"  # created from the scan


@pytest.mark.functionality
def test_an_invoice_already_in_the_dossier_stops_before_its_tiers_is_asked(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    monkeypatch.setattr(tiers, "without_tiers_twin", lambda invoice, context: {"status": "stop", "message": "doublon"})
    assert tiers.link_tiers(4)["status"] == "stop" and calls == []


@pytest.mark.functionality
def test_no_name_read_anywhere_asks_the_tiers_name(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["supplier_name"] = False
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: None)
    assert tiers.link_tiers(4)["message"] == "Facture 4 : aucun fournisseur détecté sur le scan. Quel est son nom ?"
    assert tiers.checked_values(4) == {} and calls == []


@pytest.mark.functionality
def test_a_name_only_the_check_read_and_nothing_resembles_is_asked_whether_to_create(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["supplier_name"] = False
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: (True, {"tier": "ZETA MOTORS"}, []))
    result = tiers.link_tiers(4)
    assert result["status"] == "question" and result["choices"] == {}
    assert "Aucun tiers du dossier ne ressemble à « ZETA MOTORS ». Faut-il le créer sous ce nom" in result["message"]
    assert calls == []


@pytest.mark.functionality
def test_a_name_only_the_check_read_whole_is_the_one_searched(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["supplier_name"] = "CQUET"
    monkeypatch.setattr(tiers, "flagged_tier", flagged_tier)
    bad = [{"field": "tier"}]
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: (True, {"tier": "FOCQUET SA"}, bad))
    assert tiers.link_tiers(4)["status"] == "ok"
    assert calls[0][2]["name"] == "FOCQUET SA" and "ice" not in calls[0][2]  # the ICE read was CQUET's
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: (True, {"tier": "FOCQUET SA"}, []))
    assert flagged_tier(4) is None  # not flagged: Experio's name stands


@pytest.mark.communication
def test_a_tiers_the_registry_refuses_even_unchecked_is_said_refused(scanned, monkeypatch):
    import xmlrpc.client

    from engine.tools.core.app import ToolError
    from engine.tools.invoices import tiers

    invoice, calls = scanned

    def odoo_call(model, method, values, **kw):
        raise xmlrpc.client.Fault(2, "Aucune donnée n'a pu être récupérée pour BETA SERVICES")

    monkeypatch.setattr(tiers, "odoo_call", odoo_call)
    with pytest.raises(ToolError, match="refusé le tiers `BETA SERVICES`"):
        tiers.create_tiers(invoice, "BETA SERVICES", "purchase", unchecked=True)


@pytest.mark.functionality
def test_sans_verification_with_no_name_read_is_searched_as_the_clients_words(chosen, monkeypatch):
    from engine.tools.invoices import tiers

    monkeypatch.setattr(
        tiers, "require_draft_invoice", lambda *a, **k: {"id": 4, "company_id": [778, "D"], "supplier_name": False}
    )
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: None)
    assert "ne désigne pas un seul tiers" in chosen("", "sans vérification")  # nothing to create unchecked


@pytest.mark.functionality
def test_a_name_only_the_check_read_is_linked_to_the_dossiers_tiers_of_that_very_name(scanned, monkeypatch):
    from engine.tools.invoices import tiers

    invoice, calls = scanned
    invoice["supplier_name"] = False
    monkeypatch.setattr(tiers, "stored_check", lambda invoice_id: (True, {"tier": "Nova Telecom."}, []))
    monkeypatch.setattr(tiers, "dossier_tiers", lambda company_id: [{"id": 9, "name": "NOVA TELECOM", "ice": ""}])
    result = tiers.link_tiers(4)
    assert (
        result["status"] == "ok"
        and "relié au tiers du dossier de même nom `NOVA TELECOM` (id `9`)" in result["message"]
    )
    assert calls == []
