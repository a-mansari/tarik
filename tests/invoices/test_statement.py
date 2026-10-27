"""A bank statement after its import: Experio's reading checked, its operations pointed by Experio, posted on demand."""

import io

import openpyxl
import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.experio import ExperioRefusal
from engine.tools.invoices import intake, pipeline, statement


def sheet(header):
    book = openpyxl.Workbook()
    book.active.append(header)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.functionality
def test_experios_excel_template_is_recognised_by_its_columns():
    canvas = ["Date", "Libellé", "Type d'opération", "Tiers ", "Méthode de paiement", "Débit", "Crédit", "Taux"]
    assert statement.is_canvas("releve.xlsx", sheet(canvas))
    assert not statement.is_canvas("fec.xlsx", sheet(["JournalCode", "JournalLib", "EcritureNum"]))  # a FEC
    assert not statement.is_canvas("releve.pdf", b"%PDF")


@pytest.fixture
def experio(monkeypatch):
    state = {
        "statement": {
            "id": 40,
            "state": "draft",
            "bluejay_state": "done",
            "company_id": [857, "D"],
            "journal_id": [3, "BQ"],
            "currency_id": [1, "MAD"],
            "balance_start": 1000.0,
            "balance_end": 1500.0,
            "balance_end_real": 1500.0,
            "is_complete": True,
            "is_valid": True,
            "line_ids": [1, 2, 3],
            "date": "2026-03-31",
            "is_detected": True,
        },
        "lines": [
            {"id": 1, "state": "ready", "error_msg": False, "invoice_ids": [], "partner_id": [9, "ATLAS"]},
            {"id": 2, "state": "draft", "error_msg": "The partner is mandatory for this operation.", "invoice_ids": []},
            {"id": 3, "state": "draft", "error_msg": "The partner is mandatory for this operation.", "invoice_ids": []},
        ],
        "detected": [11, 12],
        "invoices": [
            {"id": 70, "invoice_number": "FA-0012", "partner_id": [9, "ATLAS"], "type": "purchase", "refund": False}
        ],
        "in_use": [],
        "applied": [],
        "written": [],
        "post": None,
    }
    for line in state["lines"]:
        line.setdefault("partner_id", False)
        line.update(operation_type="purchase", debit=200.0, credit=0.0, payment_ref=f"VIR {line['id']}")
        line.update(payment_state="unpaid", is_reconciled=False)

    def odoo_call(model, method, *args, **kw):
        if model == statement.STATEMENT and method == "read":
            return [dict(state["statement"])]
        if model == statement.LINE and method == "read":
            return [dict(line) for line in state["lines"]]
        if model == statement.LINE and method == "write":
            state["written"].append((args[0], args[1]))
            return True
        if model == "bank.statement.line.detect.wizard":
            return [{"line_ids": state["detected"]}]
        if model == statement.LINE and method == "search_read":
            return [{"invoice_ids": state["in_use"]}]
        if model == "exp.invoice":
            return [{"is_fnp_invoice": False, **invoice} for invoice in state["invoices"]]
        if method == "search_count":
            return 1  # not the journal's first statement
        return []

    def odoo_json(model, method, ids, **kw):
        if method == "action_refresh":
            return {"res_model": "bank.statement.line.detect.wizard", "res_id": 5}
        if method == "action_apply":
            state["applied"].append(ids)
            return True
        if method == "action_post":
            if isinstance(state["post"], Exception):
                raise state["post"]
            for line in state["lines"]:
                line["state"] = "post" if line["state"] == "ready" else line["state"]
            return state["post"]

    monkeypatch.setattr(statement, "odoo_call", odoo_call)
    monkeypatch.setattr(statement, "odoo_json", odoo_json)
    monkeypatch.setattr(statement, "dry_run", lambda: False)
    monkeypatch.setattr(statement, "statement_of", lambda file_id: (40, None))
    return state


@pytest.mark.functionality
def test_a_statement_is_pointed_by_experios_detection_and_its_blocked_lines_said(experio):
    result = statement.settle_statement(8, client_message="")
    assert experio["applied"] == [[5]]  # Experio's « Auto Point », confirmed as its window's « Apply »
    assert "2 pointée(s) par la détection d'Experio" in result["message"]
    assert "1 prête(s) à comptabiliser" in result["message"]
    assert "The partner is mandatory for this operation. (2)" in result["message"]


@pytest.mark.functionality
def test_its_balances_are_said_when_they_do_not_hold(experio):
    experio["statement"].update(is_complete=False, balance_end=1400.0, is_valid=False)
    message = statement.settle_statement(8)["message"]
    assert "solde final calculé (1400.00) ne correspond pas au solde du relevé (1500.00)" in message
    assert "ne suit pas le solde final du relevé précédent" in message


@pytest.mark.functionality
def test_a_line_the_client_ties_to_an_invoice_by_its_number_gets_it(experio):
    result = statement.settle_statement(8, client_message="Le virement de 200 paie la facture FA-0012.")
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]  # once: lines 2 and 3 owe the same, not twice
    assert "1 rattachée(s) à la facture que vous avez citée" in result["message"]


@pytest.mark.functionality
def test_a_line_without_tiers_takes_the_invoices_tiers_with_it(experio):
    experio["lines"] = experio["lines"][1:2]
    statement.settle_statement(8, client_message="facture FA-0012")
    assert experio["written"] == [([2], {"invoice_ids": [(4, 70)], "partner_id": 9})]  # as _onchange_invoice_ids


@pytest.mark.functionality
@pytest.mark.parametrize(
    "in_use, kind, refund",
    [([70], "purchase", False), ([], "sale", False), ([], "purchase", True)],
    ids=["on another line", "a sale for a debit", "a purchase refund for a debit"],
)
def test_an_invoice_on_another_line_or_the_other_way_is_not_given(experio, in_use, kind, refund):
    experio["in_use"] = in_use
    experio["invoices"][0].update(type=kind, refund=refund)
    result = statement.settle_statement(8, client_message="facture FA-0012")
    assert experio["written"] == []
    assert "rattachée" not in result["message"]


@pytest.mark.functionality
def test_an_fnp_invoice_is_given_either_way(experio):
    experio["lines"] = experio["lines"][:1]
    experio["invoices"][0].update(type="sale", is_fnp_invoice=True)
    statement.settle_statement(8, client_message="facture FA-0012")
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]


@pytest.mark.functionality
def test_a_line_of_no_amount_gets_no_invoice(experio):
    experio["lines"] = experio["lines"][:1]
    experio["lines"][0].update(debit=0.0)
    statement.settle_statement(8, client_message="facture FA-0012")
    assert experio["written"] == []


@pytest.mark.functionality
def test_a_credit_line_takes_a_sale_or_a_purchase_refund(experio):
    experio["lines"] = experio["lines"][:1]
    experio["lines"][0].update(debit=0.0, credit=200.0)
    experio["invoices"][0].update(type="purchase", refund=True)
    statement.settle_statement(8, client_message="avoir FA-0012")
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]


@pytest.mark.functionality
def test_a_statement_experio_still_reads_waits(experio, monkeypatch):
    experio["statement"].update(state="pending", is_detected=False)  # bluejay_state says nothing: « pending » for good
    clock = [0.0]
    monkeypatch.setattr(statement.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(statement.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert statement.settle_statement(8, run_id="r")["status"] == "wait"


@pytest.mark.functionality
def test_posting_takes_the_ready_lines_and_lists_whats_in_suspense(experio):
    result = statement.post_statement(8)
    assert result["status"] == "ok" and "1 opération(s) comptabilisée(s), 2 restent à compléter" in result["message"]
    assert "En suspens" in result["message"] and "VIR 1 (200.00)" in result["message"]


@pytest.mark.security
def test_a_window_or_a_refusal_at_posting_stops_for_the_cabinet(experio):
    experio["post"] = {"res_model": "exp.bank.statement.duplicate.wizard", "name": "Relevé en double"}
    assert "« Relevé en double »" in statement.post_statement(8)["message"]
    experio["post"] = RuntimeError("Traceback\nUserError: Complete statement validation is required.")
    result = statement.post_statement(8)
    assert result["status"] == "stop" and result["message"].endswith("Complete statement validation is required.")


@pytest.mark.functionality
def test_a_statement_is_taken_up_again_while_read_or_when_the_client_asks_to_post(db_cabinet, monkeypatch):
    from engine.tools.core.cabinets import cabinet_db

    with cabinet_db() as db:
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, kind, statement_id) "
                "values ('s', %s, 'application/pdf', '', 'statement', %s)",
                (name, 41 if name == "c.pdf" else 40),
            )
    pipeline.record_step("r", "s", "settle_statement", {"status": "wait", "message": "m", "file_id": 1})
    pipeline.record_step("r", "s", "settle_statement", {"status": "ok", "message": "m", "file_id": 3})
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain: [41])  # 41 still to post
    assert pipeline.statements_to_resume("s", held=False) == [1]  # still being read
    assert pipeline.statements_to_resume("s", held=True) == [1, 3]  # b.pdf: merged into a.pdf's statement 40


@pytest.mark.security
def test_a_bti_statement_is_never_imported(monkeypatch):
    monkeypatch.setattr(intake, "odoo_call", lambda model, method, *a, **k: [{"code": "bti"}])
    assert intake.bank_code({"bank_id": [19, "BTI BANK"]}) in intake.UNSUPPORTED_BANKS


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("words", "merged"),
    [("C'est le même relevé en deux fichiers", True), ("Fusionnez-les", True), ("Voici les relevés de mars", False)],
)
def test_files_are_one_statement_only_on_the_clients_words(words, merged):
    assert bool(intake.SAME_STATEMENT.search(intake.normalized(words))) == merged


@pytest.mark.functionality
def test_a_template_row_becomes_a_line_experio_resolves_by_its_names(monkeypatch):
    """Tiers and operation go as text: Experio's create finds them by name (partner_value, imp_operation_id)."""
    book = openpyxl.Workbook()
    book.active.append(["Date", "Libellé", "Type d'opération", "Tiers ", "Méthode de paiement", "Débit", "Crédit"])
    book.active.append(["06/03/2023", "PRELEVEMENT RCI", "Frais Bancaire (TTC)", "RCI", "Virement", 2003.28, None])
    buffer = io.BytesIO()
    book.save(buffer)
    rows = statement.canvas_rows(buffer.getvalue())
    monkeypatch.setattr(statement, "odoo_call", lambda model, method, domain, **kw: [3])  # « Virement »: one method
    values = statement.line_values(rows[0], {})
    assert values["date"] == "2023-03-06" and values["debit"] == 2003.28 and values["credit"] == 0.0
    assert values["imp_operation_id"] == "Frais Bancaire (TTC)" and values["partner_value"] == "RCI"
    assert values["exp_payment_method_id"] == 3


@pytest.mark.security
def test_a_template_experio_refuses_leaves_no_empty_statement(monkeypatch):
    calls = []

    def odoo_call(model, method, *args, **kw):
        calls.append((model, method))
        if (model, method) == (statement.STATEMENT, "create"):
            return 44
        if (model, method) == (statement.LINE, "create"):
            raise RuntimeError("Experio refuse")
        return []

    monkeypatch.setattr(statement, "odoo_call", odoo_call)
    monkeypatch.setattr(statement, "canvas_rows", lambda data: [{"libelle": "X", "debit": 1}])
    with pytest.raises(RuntimeError):
        statement.import_canvas("r.xlsx", b"", 857, 2922)
    assert (statement.STATEMENT, "unlink") in calls


@pytest.mark.communication
def test_a_statement_without_balances_is_said_so_never_called_incomplete(experio):
    experio["statement"].update(balance_start=0.0, balance_end_real=0.0, is_complete=False, balance_end=-2216.37)
    message = statement.settle_statement(8)["message"]
    assert "ne donne pas ses soldes" in message and "une opération manque" not in message


@pytest.mark.functionality
def test_tpe_remittances_are_tied_to_their_tpe_statement_when_experio_is_sure(monkeypatch):
    written = []

    def odoo_call(model, method, *args, **kw):
        if model == "ir.module.module":
            return 1  # tpe_reconcile installed
        if model == statement.LINE and method == "read":
            return [
                {"id": 1, "operation_type": "monetary_operations", "tpe_line_id": False},
                {"id": 2, "operation_type": "monetary_operations", "tpe_line_id": False},
                {"id": 3, "operation_type": "purchase", "tpe_line_id": False},
            ]
        if model == statement.LINE and method == "write":
            written.append((args[0], args[1]))
            return True
        if model == "tpe.auto.reconcile.wizard":
            return [{"line_ids": [10, 11]}]
        if model == "tpe.auto.reconcile.wizard.line":
            return [
                {"line_id": [1, "x"], "tpe_line_id": [644, "y"], "state": "matched"},
                {"line_id": [2, "x"], "tpe_line_id": [640, "y"], "state": "ambiguous"},
            ]
        return []

    monkeypatch.setattr(statement, "odoo_call", odoo_call)
    monkeypatch.setattr(statement, "odoo_json", lambda model, method, ids, **kw: {"res_id": 5})
    monkeypatch.setattr(statement, "dry_run", lambda: False)
    assert statement.tie_tpe({"line_ids": [1, 2, 3]}, {}) == (1, 1)
    assert written == [([1], {"tpe_line_id": 644})]  # Experio's sure match only; the ambiguous one left to the cabinet


@pytest.mark.functionality
def test_a_file_without_imported_statement_is_neither_settled_nor_posted(db_cabinet):
    assert statement.statement_of(999999) == (None, None)
    assert statement.settle_statement(999999)["status"] == "ok"
    assert "pas de relevé à comptabiliser" in statement.post_statement(999999)["message"]


@pytest.mark.functionality
def test_a_tpe_statement_is_settled_and_posted_by_its_own_screens(monkeypatch):
    monkeypatch.setattr(statement, "statement_of", lambda file_id: (460, "cmi"))
    monkeypatch.setattr(statement.tpe, "read_tpe", lambda statement_id: {"state": "draft", "statement_processed": True})
    monkeypatch.setattr(statement, "read_statement", lambda statement_id: pytest.fail("not a bank statement"))
    monkeypatch.setattr(statement.tpe, "settle_tpe", lambda statement_id: {"status": "ok", "message": "settled"})
    monkeypatch.setattr(statement.tpe, "post_tpe", lambda statement_id: {"status": "ok", "message": "posted"})
    assert statement.settle_statement(8)["message"] == "settled"
    assert statement.post_statement(8)["message"] == "posted"


@pytest.mark.communication
@pytest.mark.parametrize("step", ["settle_tpe", "post_tpe"])
def test_a_tpe_statement_experio_refuses_says_its_reason(monkeypatch, step):
    def refused(statement_id):
        raise ExperioRefusal("Relevé en double", "odoo.exceptions.UserError")

    monkeypatch.setattr(statement, "statement_of", lambda file_id: (460, "cmi"))
    monkeypatch.setattr(statement.tpe, "read_tpe", lambda statement_id: {"state": "draft", "statement_processed": True})
    monkeypatch.setattr(statement.tpe, step, refused)
    run = statement.settle_statement if step == "settle_tpe" else statement.post_statement
    with pytest.raises(ToolError, match=r"n° 460 : Experio a refusé \(Relevé en"):
        run(8)


@pytest.mark.functionality
def test_no_detection_from_experio_points_nothing(experio, monkeypatch):
    monkeypatch.setattr(statement, "odoo_json", lambda model, method, ids, **kw: True)  # « No detection results »
    assert statement.auto_point({"id": 40}, {}) == 0
    assert "pointée(s)" not in statement.settle_statement(8)["message"]


@pytest.mark.security
def test_test_mode_confirms_no_detection_ties_no_invoice_and_posts_nothing(experio, monkeypatch):
    monkeypatch.setattr(statement, "dry_run", lambda: True)
    result = statement.settle_statement(8, client_message="Le virement paie la facture FA-0012.")
    assert result["message"].startswith("MODE TEST") and "2 pointée(s)" in result["message"]
    assert experio["applied"] == [] and experio["written"] == []
    posted = statement.post_statement(8)
    assert posted["message"].startswith("MODE TEST") and "seraient comptabilisées" in posted["message"]


@pytest.mark.functionality
def test_a_posted_statement_is_neither_pointed_nor_posted_again(experio, monkeypatch):
    experio["statement"]["state"] = "post"
    for line in experio["lines"]:
        line["state"] = "post"
    monkeypatch.setattr(statement, "tie_tpe", lambda *a: pytest.fail("a posted statement is not tied"))
    result = statement.settle_statement(8, client_message="facture FA-0012")
    assert experio["applied"] == [] and experio["written"] == [] and "3 déjà comptabilisée(s)" in result["message"]
    assert statement.post_statement(8)["message"] == "Relevé n° 40 : déjà comptabilisé."


@pytest.mark.communication
def test_tpe_remittances_tied_and_left_are_said(experio, monkeypatch):
    monkeypatch.setattr(statement, "tie_tpe", lambda statement_, context: (2, 1))
    message = statement.settle_statement(8)["message"]
    assert "2 versement(s) TPE rapproché(s) de leur relevé TPE" in message
    assert "1 versement(s) TPE sans relevé TPE certain : à rapprocher par le cabinet" in message


@pytest.mark.functionality
def test_without_experio_tpe_module_nothing_is_tied(monkeypatch):
    monkeypatch.setattr(statement, "odoo_call", lambda model, method, domain: 0)
    assert statement.tie_tpe({"line_ids": [1]}, {}) is None


def tpe_lines(monkeypatch, action, test_mode=False):
    written = []

    def odoo_call(model, method, *args, **kw):
        if model == "ir.module.module":
            return 1
        if model == statement.LINE and method == "read":
            return [{"id": 1, "operation_type": "monetary_operations", "tpe_line_id": False}]
        if model == statement.LINE and method == "write":
            written.append(args)
        if model == "tpe.auto.reconcile.wizard":
            return [{"line_ids": [10]}]
        return [{"line_id": [1, "x"], "tpe_line_id": [644, "y"], "state": "matched"}]

    monkeypatch.setattr(statement, "odoo_call", odoo_call)
    monkeypatch.setattr(statement, "odoo_json", lambda model, method, ids, **kw: action)
    monkeypatch.setattr(statement, "dry_run", lambda: test_mode)
    return written


@pytest.mark.functionality
def test_experio_finding_no_tpe_match_leaves_the_remittances(monkeypatch):
    written = tpe_lines(monkeypatch, True)
    assert statement.tie_tpe({"line_ids": [1]}, {}) == (0, 1) and written == []


@pytest.mark.security
def test_test_mode_ties_no_tpe_remittance(monkeypatch):
    written = tpe_lines(monkeypatch, {"res_id": 5}, test_mode=True)
    assert statement.tie_tpe({"line_ids": [1]}, {}) == (1, 0) and written == []


def first_statement(monkeypatch, account, lines):
    def odoo_call(model, method, *args, **kw):
        if method == "search_count":
            return 0  # the journal's first statement
        if model == "exp.journal":
            return [{"default_account_id": account}]
        assert ("move_id.move_category", "=", "carry_forward") in args[0]
        return lines

    monkeypatch.setattr(statement, "odoo_call", odoo_call)


@pytest.mark.communication
@pytest.mark.parametrize(
    ("account", "day", "lines", "note"),
    [
        ([5141, "BQ"], "2026-03-31", [{"balance": 900.0, "date": "2026-01-01"}], "(1000.00) diffère de l'à-nouveau "),
        ([5141, "BQ"], "2026-03-31", [{"balance": -1000.0, "date": "2026-01-01"}], ""),  # the same, credit side
        ([5141, "BQ"], "2026-03-31", [], ""),  # no à-nouveau: nothing to compare
        ([5141, "BQ"], "2026-03-31", [{"balance": 900.0, "date": "2025-01-01"}], ""),  # another exercice's only
        (False, "2026-03-31", [{"balance": 9.0, "date": "2026-01-01"}], ""),  # no account on the journal
        ([5141, "BQ"], False, [{"balance": 9.0, "date": "2026-01-01"}], ""),  # no date read
    ],
)
def test_the_first_statement_of_a_journal_is_checked_against_its_carry_forward(monkeypatch, account, day, lines, note):
    first_statement(monkeypatch, account, lines)
    found = statement.opening_note({"id": 40, "journal_id": [3, "BQ"], "date": day, "balance_start": 1000.0}, {})
    assert note in found if note else found == ""


@pytest.mark.functionality
def test_a_template_row_with_its_rate_and_currency_takes_them_when_experio_finds_one(monkeypatch):
    found = {"exp.payment.method": [3, 4], "res.currency": [2]}  # two methods of that name: none taken
    monkeypatch.setattr(statement, "odoo_call", lambda model, method, domain, **kw: found[model])
    row = {"date": "06/03/2023", "methode de paiement": "Virement", "devise": "EUR", "taux de change": "10,85"}
    values = statement.line_values(row, {})
    assert values["exchange_rate"] == 10.85 and values["currency_id"] == 2 and "exp_payment_method_id" not in values


@pytest.mark.functionality
def test_a_template_is_imported_into_a_new_statement_of_the_journal(monkeypatch):
    calls = []

    def odoo_call(model, method, *args, **kw):
        calls.append((model, method, args, kw))
        return 44 if method == "create" and model == statement.STATEMENT else [1]

    monkeypatch.setattr(statement, "odoo_call", odoo_call)
    monkeypatch.setattr(statement, "canvas_rows", lambda data: [{"date": "06/03/2023", "libelle": "X", "debit": 1}])
    assert statement.import_canvas("releve mars.xlsx", b"", 857, 2922) == 44
    assert calls[0][2] == ({"journal_id": 2922, "name": "releve mars"},)
    assert calls[1][3]["context"] == {
        "allowed_company_ids": [857],
        "from_xlsx_import": True,
        "default_statement_id": 44,
    }


@pytest.mark.functionality
def test_an_empty_template_makes_no_statement(monkeypatch):
    monkeypatch.setattr(statement, "odoo_call", lambda *a, **k: pytest.fail("nothing made"))
    monkeypatch.setattr(statement, "canvas_rows", lambda data: [])
    with pytest.raises(RuntimeError, match="aucune ligne"):
        statement.import_canvas("r.xlsx", b"", 857, 2922)
