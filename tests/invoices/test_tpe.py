"""A TPE statement: its network read, imported through Experio's own TPE upload, its lines given Experio's
operations, its card sales' client asked, posted on demand."""

import pytest

from engine.tools.invoices import tpe

HEADER = """ATTIJARI PAYMENT S.A au capital de 35.000.000 DH,
RELEVE DES OPERATIONS MONETIQUES
VEUILLEZ TROUVER CI-APRES LE DECOMPTE DE VOS REMISES CARTES SAUF ERREUR OU OMISSION
Email: attijaripayment@attijariwafa.com
V VISA M MASTERCARD D DINERS J JCB C CMI U CUP"""


@pytest.mark.functionality
def test_a_tpe_statement_and_its_network_are_read_on_its_header(monkeypatch):
    monkeypatch.setattr(tpe, "first_page_text", lambda raw: HEADER)
    assert tpe.tpe_network(b"", "") == (True, "attijari")
    bank = "ATTIJARIWAFA BANK RELEVE DE COMPTE\n02/08 REMISE CMI 1200,00\n03/08 VIREMENT RECU"
    monkeypatch.setattr(tpe, "first_page_text", lambda raw: bank)
    assert tpe.tpe_network(b"", "") == (False, None)  # a bank statement's line of TPE remittance is no TPE statement


@pytest.mark.functionality
def test_a_scan_is_a_tpe_statement_by_the_clients_words(monkeypatch):
    monkeypatch.setattr(tpe, "first_page_text", lambda raw: "")
    assert tpe.tpe_network(b"", "Voici le relevé TPE NAPS d'août") == (True, "naps")
    assert tpe.tpe_network(b"", "Voici le relevé TPE") == (True, None)  # its network then asked


@pytest.mark.functionality
def test_the_network_asked_is_answered_by_its_number():
    asked = tpe.network_question("tpe.pdf")
    choices = {str(k): v for k, v in asked["choices"].items()}  # as record_step keeps them
    assert tpe.network_answered(choices, 2) == "naps"
    assert tpe.network_answered({"7": "BQ ATTIJARI"}, 7) is None  # a bank account's question


@pytest.mark.functionality
def test_each_line_gets_the_operation_of_experios_own_suffix():
    def named(label):
        return tpe.operation_named({"payment_ref": label})

    assert named("REMISE TPE N. 000820 TOTAL REMISE") == "VENTE"
    assert named("REMISE TPE N. 000820 COMMISSION HT") == "FRAIS BANCAIRES"
    assert named("REMISE TPE N. 000820 COMMISSION TVA") == "TAXE BANCAIRE"
    assert named("REMISE TPE N. 000820 NET REMISE") == "SOLDE NET REMISE"
    assert named("REMISE TPE N. 000820 28/08/2026 STAN 003494") == "VENTE"  # a transaction, in detail


def line(id, label, debit=0.0, credit=0.0, **values):
    return {
        "id": id,
        "payment_ref": f"REMISE TPE N. 000820 {label}",
        "operation_id": False,
        "partner_id": False,
        "invoice_ids": [],
        "state": "draft",
        "error_msg": "The operation must be defined for bank statement lines",
        "debit": debit,
        "credit": credit,
        **values,
    }


@pytest.fixture
def experio(monkeypatch):
    state = {
        "statement": {
            "id": 460,
            "state": "draft",
            "statement_processed": True,
            "company_id": [857, "D"],
            "journal_id": [3, "BQ"],
            "type": "attijari",
            "line_ids": [1, 2, 3, 4],
            "name": "RIAD",
        },
        "lines": [
            line(1, "TOTAL REMISE", credit=2400.0),
            line(2, "COMMISSION HT", debit=38.88),
            line(3, "COMMISSION TVA", debit=3.89),
            line(4, "NET REMISE", debit=2357.23),
        ],
        "operations": [
            {"id": 1188, "name": "VENTE", "type": "normal", "bank_type": False, "account_id": [5, "34210000 Clients"]},
            {"id": 1190, "name": "VENTE", "type": "normal", "bank_type": False, "account_id": [6, "34210002 ACCES"]},
            {"id": 1189, "name": "SOLDE NET REMISE", "type": "funds_transfer", "bank_type": False, "account_id": [7]},
            {"id": 1269, "name": "TAXE BANCAIRE", "type": "bank_fee", "bank_type": "tax", "account_id": False},
        ],
        "created": [],
        "partners": [{"id": 77, "name": "CLIENTS DIVERS"}],
        "post": None,
        "confirmed": [],
    }

    def odoo_call(model, method, *args, **kw):
        if model == tpe.STATEMENT and method == "read":
            return [dict(state["statement"])]
        if model == tpe.LINE and method == "read":
            return [dict(entry) for entry in state["lines"]]
        if model == tpe.LINE and method == "write":
            for entry in state["lines"]:
                if entry["id"] in args[0]:
                    entry.update({k: [v, "x"] for k, v in args[1].items()})
                    entry["state"] = "ready"
            return True
        if model == tpe.OPERATION and method == "search_read":
            return [dict(o) for o in state["operations"]]
        if model == tpe.OPERATION and method == "create":
            state["created"] += args[0]
            for i, values in enumerate(args[0]):
                state["operations"].append({**values, "id": 2000 + i, "account_id": [values["account_id"], ""]})
            return [2000]
        if model == "exp.account":
            return [300]  # 61471000
        if model == "exp.tax":
            return [4760]
        if model == "ir.model.data":
            return [{"res_id": 52}]
        if model == "exp.partner" and method == "search_read":
            return [dict(p) for p in state["partners"]]
        if model == "exp.partner" and method == "read":
            return [{"customer_account_id": [6, "34210002"]}]
        if model == "res.company":
            return [{"monetary_operations_ignore_remise": state.get("ignore_remise", False)}]
        return []

    def odoo_json(model, method, ids, **kw):
        if method == "action_post":
            if isinstance(state["post"], Exception):
                raise state["post"]
            for entry in state["lines"]:
                entry["state"] = "post"
            return state["post"]
        if method == "action_confirm_anyway":
            state["confirmed"].append(ids)
            return state.get("after_confirm", True)

    monkeypatch.setattr(tpe, "odoo_call", odoo_call)
    monkeypatch.setattr(tpe, "odoo_json", odoo_json)
    monkeypatch.setattr(tpe, "dry_run", lambda: False)
    return state


@pytest.mark.functionality
def test_a_missing_operation_is_made_as_experio_makes_it(experio):
    tpe.settle_tpe(460)
    made = experio["created"]
    assert made == [
        {
            "name": "FRAIS BANCAIRES",
            "bank_type": "frais_without_tax",
            "type": "bank_fee",
            "company_id": 857,
            "tax_id": 4760,
            "code_id": 52,
            "account_id": 300,
        }
    ]


@pytest.mark.functionality
def test_sales_without_client_are_asked_once_for_the_statement(experio):
    result = tpe.settle_tpe(460)
    assert result["status"] == "question"
    assert "1 vente par carte n'a pas de client" in result["message"]
    operations = {entry["id"]: entry["operation_id"][0] for entry in experio["lines"] if entry["operation_id"]}
    assert operations == {1: 1188, 2: 2000, 3: 1269, 4: 1189}  # the sale on the dossier's first VENTE


@pytest.mark.functionality
def test_the_client_named_gets_the_sales_and_its_own_sale_operation(experio, monkeypatch):
    monkeypatch.setattr(tpe, "cabinet_db", fake_db)
    result = tpe.answer_client(8, "Mettez-les sur clients divers", "clients divers")
    assert result["status"] == "ok"
    sale = experio["lines"][0]
    assert sale["partner_id"][0] == 77 and sale["operation_id"][0] == 1190  # VENTE of its customer account
    settled = tpe.settle_tpe(460)
    assert settled["status"] == "ok"
    assert (
        "total remise 2400.00, commissions HT 38.88, TVA sur commissions 3.89, net remise 2357.23"
        in (settled["message"])
    )


class fake_db:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        return self

    def fetchone(self):
        return (460,)


@pytest.mark.functionality
def test_posting_confirms_experios_network_change_warning(experio):
    experio["post"] = {"res_model": tpe.JOURNAL_WARNING, "res_id": 3}
    result = tpe.post_tpe(460)
    assert experio["confirmed"] == [[3]]
    assert result["status"] == "ok" and "j'ai comptabilisé 4 lignes" in result["message"]
    assert "confirmé" in result["message"]


@pytest.mark.functionality
def test_a_duplicate_statement_stops(experio):
    experio["post"] = Exception("Ce relevé TPE ne peut pas être comptabilisé, car un relevé en doublon existe")
    result = tpe.post_tpe(460)
    assert result["status"] == "stop" and "doublon" in result["message"]


@pytest.mark.functionality
def test_the_import_sends_the_network_as_experios_screen_does(monkeypatch):
    calls = []

    def odoo_call(model, method, values, **kw):
        calls.append((method, values))
        return 9

    def odoo_json(model, method, ids, documents, **kw):
        calls.append((method, documents))
        return [460]

    monkeypatch.setattr(tpe, "odoo_call", odoo_call)
    monkeypatch.setattr(tpe, "odoo_json", odoo_json)
    assert tpe.import_tpe("riad.pdf", b"%PDF", 857, 3, "attijari") == 460
    document = calls[1][1][0]
    assert calls[1][0] == "action_add_documents_from_javascript"
    assert document["type"] == "attijari" and document["journal_id"] == 3 and document["name"] == "riad"


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("found", "journal"), [([{"journal_id": [3, "BQ"]}], 3), ([{"journal_id": False}], None), ([], None)]
)
def test_the_next_tpe_statement_goes_to_the_journal_of_the_last_one(monkeypatch, found, journal):
    monkeypatch.setattr(tpe, "odoo_call", lambda model, method, domain, fields, limit: found)
    assert tpe.last_journal(857) == journal


@pytest.mark.communication
@pytest.mark.parametrize("made", [[], None, [460, 461]])
def test_an_upload_that_does_not_make_exactly_one_statement_is_refused(monkeypatch, made):
    monkeypatch.setattr(tpe, "odoo_call", lambda *a, **k: 9)
    monkeypatch.setattr(tpe, "odoo_json", lambda *a, **k: made)
    with pytest.raises(RuntimeError, match="expected one statement"):
        tpe.import_tpe("riad.pdf", b"%PDF", 857, 3, "cmi")


@pytest.mark.functionality
def test_a_tpe_statement_is_being_read_until_experio_processed_it():
    assert tpe.being_read({"state": "pending", "statement_processed": False})
    assert not tpe.being_read({"state": "pending", "statement_processed": True})
    assert not tpe.being_read({"state": "draft", "statement_processed": False})


def without_accounts(monkeypatch):
    inner = tpe.odoo_call
    monkeypatch.setattr(
        tpe, "odoo_call", lambda model, *a, **k: [] if model == "exp.account" else inner(model, *a, **k)
    )


@pytest.mark.functionality
def test_an_operation_whose_account_the_dossier_lacks_stops_for_the_cabinet(experio, monkeypatch):
    without_accounts(monkeypatch)
    result = tpe.settle_tpe(460)
    assert (
        result["status"] == "stop" and "l'opération TPE `FRAIS BANCAIRES` ni son compte (`61471`)" in result["message"]
    )
    assert experio["created"] == []


@pytest.mark.security
def test_test_mode_makes_no_operation_and_writes_no_line(experio, monkeypatch):
    monkeypatch.setattr(tpe, "dry_run", lambda: True)
    result = tpe.settle_tpe(460, "clients divers")
    assert experio["created"] == [] and all(not entry["operation_id"] for entry in experio["lines"])
    assert result["message"].startswith("MODE TEST") and "J'ai créé dans le dossier" in result["message"]


@pytest.mark.functionality
def test_the_client_named_at_settling_gets_the_sales_and_lines_left_are_said(experio):
    experio["lines"].append(line(5, "FRAIS DIVERS", debit=5.0, error_msg="<p>Compte manquant</p>"))
    experio["lines"].append(line(6, "TOTAL REMISE", credit=1.0, state="post"))
    experio["ignore_remise"] = True
    result = tpe.settle_tpe(460, "clients divers")
    assert (
        result["status"] == "ok" and "ventes par carte sont rattachées au client `CLIENTS DIVERS`" in result["message"]
    )
    assert "- 1 ligne à compléter dans Experio : compte manquant." in result["message"]
    assert "1 est déjà comptabilisée" in result["message"] and "Ignorer la ligne de solde remise" in result["message"]
    assert not experio["lines"][5]["operation_id"]  # a posted line is never written


@pytest.mark.functionality
def test_several_clients_matching_are_listed_never_chosen(experio):
    experio["partners"] = [{"id": 77, "name": "CLIENTS DIVERS"}, {"id": 78, "name": "CLIENTS DIVERS SUD"}]
    client, found = tpe.find_client(857, "divers", {})
    assert client is None and len(found) == 2
    assert tpe.find_client(857, "clients divers", {})[0]["id"] == 77  # exactly its name
    assert tpe.find_client(857, "  ", {}) == (None, [])


@pytest.mark.communication
def test_a_client_named_but_unknown_is_asked_again_saying_so(experio, monkeypatch):
    monkeypatch.setattr(tpe, "cabinet_db", fake_db)
    experio["partners"] = []
    result = tpe.answer_client(8, "Mettez-les sur Riad Atlas", "")
    assert (
        result["status"] == "question"
        and "Aucun client « Mettez-les sur Riad Atlas » dans le dossier" in (result["message"])
    )


@pytest.mark.functionality
def test_the_client_own_words_name_the_client_when_the_agent_read_none(experio, monkeypatch):
    monkeypatch.setattr(tpe, "cabinet_db", fake_db)
    asked = []
    inner = tpe.odoo_call

    def odoo_call(model, method, *args, **kw):
        if model == "exp.partner" and method == "search_read":
            asked.append(args[0][1][2])
            return [{"id": 77, "name": "CLIENTS DIVERS"}] if args[0][1][2] == "clients divers" else []
        return inner(model, method, *args, **kw)

    monkeypatch.setattr(tpe, "odoo_call", odoo_call)
    result = tpe.answer_client(8, "clients divers", "autre")
    assert result["status"] == "ok" and asked == ["autre", "clients divers"]


@pytest.mark.functionality
def test_a_posted_tpe_statement_is_not_posted_again(experio):
    experio["statement"]["state"] = "post"
    assert tpe.post_tpe(460)["message"] == "Relevé TPE n° 460 : déjà comptabilisé."


@pytest.mark.security
def test_test_mode_posts_no_tpe_statement(experio, monkeypatch):
    monkeypatch.setattr(tpe, "dry_run", lambda: True)
    result = tpe.post_tpe(460)
    assert result["message"].startswith("MODE TEST") and all(entry["state"] == "draft" for entry in experio["lines"])


@pytest.mark.security
def test_a_window_experio_opens_after_the_network_warning_is_confirmed_still_stops(experio):
    experio["post"] = {"res_model": tpe.JOURNAL_WARNING, "res_id": 3}
    experio["after_confirm"] = {"res_model": "exp.monetary.statement.duplicate", "name": "Doublon possible"}
    result = tpe.post_tpe(460)
    assert experio["confirmed"] == [[3]] and result["status"] == "stop" and "« Doublon possible »" in result["message"]


@pytest.mark.security
def test_another_window_at_posting_stops_for_the_cabinet(experio):
    experio["post"] = {"res_model": "exp.monetary.statement.duplicate", "name": "Doublon possible"}
    result = tpe.post_tpe(460)
    assert result["status"] == "stop" and "« Doublon possible »" in result["message"] and experio["confirmed"] == []
