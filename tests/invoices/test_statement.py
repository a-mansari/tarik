"""A bank statement after its import: Experio's reading checked, its operations pointed by Experio, posted on demand."""

import io

import openpyxl
import pytest

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
        if model == "exp.invoice":
            return [{"id": 70, "invoice_number": "FA-0012"}]
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
    assert experio["written"][0] == ([1], {"invoice_ids": [(4, 70)]})
    assert "rattachée(s) à la facture que vous avez citée" in result["message"]


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
