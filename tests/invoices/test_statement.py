"""A bank statement after its import: Experio's reading checked, its operations pointed by Experio, posted on demand."""

import io
from datetime import date

import openpyxl
import pytest

from engine import pointage
from engine.tools.core import experio as core
from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import ExperioRefusal
from engine.tools.core.queries import Q
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
        "invoices": [{"id": 70, "invoice_number": "FA-0012", "amount_residual": 200.0}],
        "in_use": [],  # invoices on a line...
        "in_use_line": 99,  # ...of another statement
        "picker": None,  # the invoices the lines' pickers offer, None: all
        "form": {70: {"partner_id": 9}},  # {invoice: what the line's form fills once it is chosen, or its error}
        "typed": [],
        "searched": [],
        "applied": [],
        "written": [],
        "post": None,
    }
    state.update(
        history=[],
        open=[40],
        owed=[],
        owing=[],
        partners=[],
        operations=[],
        answer=None,
        asked=[],
        declared=0,
        owner={},
        generic=[],
        by_number=[],
        labelled=[],
        created=[],
        created_written=[],
        by_amount=[],
        accepts=True,
    )
    for line in state["lines"]:
        line.setdefault("partner_id", False)
        line.setdefault("operation_id", [1, "Achat Local"])
        line.setdefault("exp_payment_method_id", [4, "Virement Bancaire"])
        line.setdefault("partner_id_domain", "[('company_id', '=', 857), ('is_supplier', '=', True)]")
        line.update(
            operation_type="purchase", debit=200.0, credit=0.0, payment_ref=f"VIR {line['id']}", date="2026-03-15"
        )
        line.update(payment_state="unpaid", is_reconciled=False, invoice_ids_domain="[('company_id', '=', 857)]")

    def odoo_call(model, method, *args, **kw):
        if model == statement.STATEMENT and method == "search":
            return list(state["open"])
        if model == statement.STATEMENT and method == "read":
            return [dict(state["statement"], id=i) for i in args[0]]
        if model == statement.LINE and method == "read":
            return [dict(line) for line in state["lines"]]
        if model == statement.LINE and method == "write":
            state["written"].append((args[0], args[1]))
            return True
        if model == "bank.statement.line.detect.wizard":
            return [{"line_ids": state["detected"]}]
        if model == statement.LINE and method == "search_read" and kw.get("order"):  # the dossier's past lines
            return [dict(row) for row in state["history"]]
        if model == statement.LINE and method == "search_read":
            return [{"id": state["in_use_line"], "invoice_ids": state["in_use"]}]
        by_label = ("invoice_number", "invoice_ref")
        named = [t for t in args[0] if isinstance(t, tuple) and t[0] in by_label and t[1] == "ilike"]
        if model == "exp.invoice" and method == "search_read" and named:  # the invoices a label's number names
            return [{**row, named[0][0]: row["invoice_number"]} for row in state["labelled"]]
        if model == "exp.invoice" and method == "read":  # an invoice's tiers
            return [{"id": i, "partner_id": state["owner"].get(i, [9, "ATLAS"])} for i in args[0]]
        if model == "exp.invoice":
            terms = {term[0]: term[2] for term in args[0]}
            if method == "search" and "id" not in terms:  # the invoices owing a line's amount (owed_invoices)
                state["owing"].append(args[0])
                return list(state["owed"])
            state["searched"].append((method, args[0]))
            if method == "search":
                allowed = state["picker"] if state["picker"] is not None else terms["id"]
                return [i for i in terms["id"] if i in allowed]
            return [invoice for invoice in state["invoices"] if invoice["amount_residual"] in terms["amount_residual"]]
        if model == "exp.move.line":  # the ledger: entries quoting a number, then supplier entries of an amount
            quoting = any(term[0] == "name" for term in args[0] if isinstance(term, tuple))
            return list(state["by_number"] if quoting else state["by_amount"])
        if model == "exp.partner" and method == "create":  # a tiers made from a label's name
            state["created"].append(args[0])
            return 77
        if model == "exp.partner" and method == "write":
            state["created_written"].append((args[0], args[1]))
            return True
        if model == "exp.partner" and method == "read":
            return [{"third_party_supplier_account": False, "third_party_customer_account": False}]
        if model == "exp.partner" and method == "search_count":  # the line's tiers picker accepts the supplier
            return 1 if state["accepts"] else 0
        if model == "exp.partner" and method == "search":  # the dossier's « CLIENTS DIVERS »
            return list(state["generic"])
        if model == "exp.partner":
            return [dict(partner) for partner in state["partners"]]
        if model == "bank.operation.type":
            return [dict(operation) for operation in state["operations"]]
        if model == "exp.payment.method":
            return [{"id": 2, "name": "Chèque"}, {"id": 4, "name": "Virement Bancaire"}]
        if model == "exp.deduction.table":  # the TVA declarations owing exactly a line's amount
            return state["declared"]
        if method == "search_count":
            return 1  # not the journal's first statement
        return []

    def odoo_json(model, method, ids, *args, **kw):
        if method == "onchange":
            state["typed"].append((ids, (dict(args[0]), *args[1:])))
            if "operation_id" in args[0]:  # the operation's own tiers, filled by its onchange
                return {"value": state.get("operation_partner", {})}
            invoice_id = args[0]["invoice_ids"][0][2][0]
            filled = state["form"].get((ids[0], invoice_id), state["form"].get(invoice_id, {}))  # by line or invoice
            if isinstance(filled, Exception):
                raise filled
            return {"value": filled}
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
    monkeypatch.setattr(core, "odoo_json", odoo_json)  # the line's form, typed in by core.type_in
    monkeypatch.setattr(statement, "dry_run", lambda: False)

    def ask_model(system, pages, instruction, what, **kw):
        state["asked"].append(instruction)
        answer = state["answer"]
        if isinstance(answer, tuple):  # one answer per call
            answer = answer[len(state["asked"]) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer or {"lines": []}

    monkeypatch.setattr(statement, "ask_model", ask_model)
    monkeypatch.setattr(statement, "tool_prompt", lambda name: name)
    monkeypatch.setattr(statement, "statement_of", lambda file_id: (40, None))
    return state


def pointed(client_message=""):
    """Bank statement 40's pointing steps in the graph's order (engine.pointage), then its report: as the graph runs
    them, the first step not ok ends it (its result returned), else the report's result."""
    done = []
    for tool in pointage.STEPS.values():
        words = {"client_message": client_message} if tool == "tie_cited_invoices" else {}
        result = getattr(statement, tool)(40, **words)
        if result["status"] != "ok":
            return result
        done.append(result)
    said = [result["said"] for result in done if result.get("said")]
    return statement.report_statement(40, said=said, notes=[result["note"] for result in done if result.get("note")])


@pytest.mark.functionality
def test_a_statement_is_pointed_by_experios_detection_and_its_blocked_lines_said(experio):
    result = pointed(client_message="")
    assert experio["applied"] == [[5]]  # Experio's « Auto Point », confirmed as its window's « Apply »
    assert "2 pointée(s) par la détection d'Experio" in result["message"]
    assert "1 prête(s) à comptabiliser" in result["message"]
    assert "The partner is mandatory for this operation. (2)" in result["message"]


@pytest.mark.functionality
def test_its_balances_are_said_when_they_do_not_hold(experio):
    experio["statement"].update(is_complete=False, balance_end=1400.0, is_valid=False)
    message = pointed()["message"]
    assert "solde final calculé (1400.00) ne correspond pas au solde du relevé (1500.00)" in message
    assert "ne suit pas le solde final du relevé précédent" in message


@pytest.mark.functionality
def test_a_line_the_client_ties_to_an_invoice_by_its_number_gets_it(experio):
    result = pointed(client_message="Le virement de 200 paie la facture FA-0012.")
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]  # once: lines 2 and 3 owe the same, not twice
    named = [("company_id", "=", 857), ("state", "in", ["post", "export"]), ("invoice_number", "!=", False)]
    picker = [("company_id", "=", 857), ("id", "in", [70])]  # the line's form's picker, asked about the named ones
    assert experio["searched"] == [("search_read", [*named, ("amount_residual", "in", [200.0])]), ("search", picker)]
    assert "1 rattachée(s) à la facture que vous avez citée" in result["message"]


@pytest.mark.functionality
def test_a_line_without_tiers_takes_the_invoices_tiers_with_it(experio):
    experio["lines"] = experio["lines"][1:2]
    pointed(client_message="facture FA-0012")
    assert experio["written"] == [([2], {"invoice_ids": [(4, 70)], "partner_id": 9})]  # as _onchange_invoice_ids


@pytest.mark.functionality
def test_the_invoice_is_tried_in_the_lines_form_first(experio):
    experio["lines"] = experio["lines"][:1]
    pointed(client_message="facture FA-0012")
    spec = {"invoice_ids": {}, "partner_id": {}}
    assert experio["typed"] == [([1], ({"invoice_ids": [(6, 0, [70])]}, ["invoice_ids"], spec))]


@pytest.mark.functionality
def test_an_invoice_on_another_line_is_not_given(experio):
    experio["in_use"] = [70]
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == [] and experio["typed"] == []
    assert (
        "Facture(s) citée(s) non rattachée(s) : FA-0012 : déjà sur une opération d'un autre relevé."
        in result["message"]
    )


@pytest.mark.functionality
def test_an_invoice_auto_point_put_on_this_statement_is_neither_given_again_nor_said(experio):
    experio.update(in_use=[70], in_use_line=1)
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == [] and "non rattachée" not in result["message"]


SAME_ACCOUNT = "Les factures sélectionnées doivent avoir le même compte que leur opération"


@pytest.mark.functionality
def test_an_invoice_the_lines_form_refuses_is_not_given_and_its_reason_said(experio):
    experio["form"] = {70: ExperioRefusal(SAME_ACCOUNT, "odoo.exceptions.UserError")}
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == []
    assert f"FA-0012 : Experio la refuse sur l'opération « VIR 1 » ({SAME_ACCOUNT})." in result["message"]


@pytest.mark.functionality
def test_of_two_invoices_named_the_one_the_form_accepts_is_given(experio):
    experio["lines"] = experio["lines"][1:2]
    experio["invoices"].append({"id": 71, "invoice_number": "FA-0013", "amount_residual": 200.0})
    experio["form"] = {70: ExperioRefusal(SAME_ACCOUNT, "odoo.exceptions.UserError"), 71: {}}
    result = pointed(client_message="FA-0012 ou FA-0013")
    assert experio["written"] == [([2], {"invoice_ids": [(4, 71)]})]  # the form filled no tiers: none written
    assert "FA-0012 : " in result["message"]


@pytest.mark.functionality
def test_a_line_two_invoices_fit_gets_none_and_no_more_is_tried(experio):
    experio["lines"] = experio["lines"][:1]
    experio["invoices"] += [
        {"id": 71, "invoice_number": "FA-0013", "amount_residual": 200.0},
        {"id": 72, "invoice_number": "FA-0014", "amount_residual": 200.0},
    ]
    result = pointed(client_message="FA-0012 FA-0013 FA-0014")
    assert experio["written"] == [] and len(experio["typed"]) == 2
    for number in ("FA-0012", "FA-0013", "FA-0014"):  # FA-0014 was not tried: the same reason
        assert f"{number} : une autre facture citée convient aussi à l'opération « VIR 1 »" in result["message"]


@pytest.mark.functionality
def test_an_invoice_one_line_refuses_goes_to_the_next_that_accepts_it(experio):
    experio["lines"] = experio["lines"][:2]
    experio["form"][(1, 70)] = ExperioRefusal(SAME_ACCOUNT, "odoo.exceptions.UserError")
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == [([2], {"invoice_ids": [(4, 70)], "partner_id": 9})]
    assert "non rattachée" not in result["message"]


@pytest.mark.functionality
def test_a_named_invoice_owing_no_open_lines_amount_is_neither_given_nor_said(experio):
    experio["invoices"][0]["amount_residual"] = 5000.0  # a partial payment included
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == [] and experio["typed"] == [] and "FA-0012" not in result["message"]


@pytest.mark.functionality
def test_an_invoice_the_lines_picker_leaves_out_is_said_not_given(experio):
    experio["picker"] = set()  # another tiers' or the other way's
    result = pointed(client_message="facture FA-0012")
    assert experio["written"] == [] and experio["typed"] == []
    assert "FA-0012 : l'opération de son montant ne la propose pas (autre tiers ou autre sens)." in result["message"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "number, words",
    [
        ("FAC 2024 015", "réglé FAC2024015"),
        ("FA12", "facture FA 12"),
        ("FNP2024", "fnp 2024"),
        ("2024 015", "facture 2024015"),
        ("2024015", "facture 2024015"),
    ],
    ids=["spaces stored", "spaces written", "letters then digits", "digits spaced when stored", "digits together"],
)
def test_an_invoice_number_is_cited_its_spaces_aside_as_experio_compares_it(experio, number, words):
    experio["lines"] = experio["lines"][:1]
    experio["invoices"][0]["invoice_number"] = number
    pointed(client_message=words)
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "number, words",
    [
        ("15", "virement du 15/10"),
        ("A15", "passé à 15 heures"),
        ("Facture", "facture de mars"),
        ("1510", "virement du 15/10/2024"),
    ],
    ids=["three digits", "three characters", "no digit", "digits spliced from a date"],
)
def test_a_short_or_digitless_or_spliced_number_is_never_cited(experio, number, words):
    experio["invoices"][0]["invoice_number"] = number
    pointed(client_message=words)
    assert experio["written"] == [] and experio["searched"][-1][0] == "search_read"


@pytest.mark.functionality
def test_a_line_without_label_is_said_by_its_date_and_amount(experio):
    experio["lines"] = experio["lines"][:1]
    experio["lines"][0]["payment_ref"] = False
    experio["form"] = {70: ExperioRefusal(SAME_ACCOUNT, "odoo.exceptions.UserError")}
    result = pointed(client_message="facture FA-0012")
    assert "l'opération « 2026-03-15 200.00 »" in result["message"]


@pytest.mark.functionality
def test_an_invoice_number_counts_only_as_whole_words(experio):
    experio["lines"] = experio["lines"][:1]
    experio["invoices"].insert(0, {"id": 69, "invoice_number": "FA-001", "amount_residual": 200.0})
    pointed(client_message="facture FA-0012")  # « fa 001 » is not « fa 0012 »
    assert experio["written"] == [([1], {"invoice_ids": [(4, 70)]})]


@pytest.mark.functionality
def test_an_experio_error_other_than_a_refusal_stops_the_step(experio):
    experio["form"] = {70: ExperioRefusal("Access denied", "odoo.exceptions.AccessError")}
    with pytest.raises(ToolError, match=r"n° 40 : Experio a refusé \(Access denied\)"):
        pointed(client_message="facture FA-0012")


@pytest.mark.functionality
def test_a_line_of_no_amount_gets_no_invoice(experio):
    experio["lines"] = experio["lines"][:1]
    experio["lines"][0].update(debit=0.0)
    pointed(client_message="facture FA-0012")
    assert experio["written"] == []


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
    message = pointed()["message"]
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
    assert statement.settle_statement(8)["message"] == "settled"  # TPE
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
    assert "pointée(s)" not in pointed()["message"]


@pytest.mark.security
def test_test_mode_confirms_no_detection_ties_no_invoice_and_posts_nothing(experio, monkeypatch):
    monkeypatch.setattr(statement, "dry_run", lambda: True)
    result = pointed(client_message="Le virement paie la facture FA-0012.")
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
    result = pointed(client_message="facture FA-0012")
    assert experio["applied"] == [] and experio["written"] == [] and "3 déjà comptabilisée(s)" in result["message"]
    assert statement.post_statement(8)["message"] == "Relevé n° 40 : déjà comptabilisé."


@pytest.mark.communication
def test_tpe_remittances_tied_and_left_are_said(experio, monkeypatch):
    monkeypatch.setattr(statement, "tie_tpe", lambda statement_, context: (2, 1))
    message = pointed()["message"]
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


def unpointed(experio, **line):
    """Line 2 left by Auto Point: no operation (unless given), no tiers, no payment method."""
    experio["lines"][1].update(operation_id=False, operation_type=False, exp_payment_method_id=False, **line)
    experio["operations"] = [
        {"id": 13136, "name": "Achat Local", "type": "purchase", "payment_type": "outbound", "account_id": [1, "4411"]},
        {"id": 13131, "name": "Vente Local", "type": "sale", "payment_type": "inbound", "account_id": [2, "3421"]},
        {"id": 13156, "name": "Frais bancaires", "type": "bank_fee", "payment_type": False, "account_id": [3, "6147"]},
    ]
    for operation in experio["operations"]:
        operation["error_msg"] = False
    return experio["lines"][1]


@pytest.mark.functionality
def test_a_line_without_operation_gets_the_one_the_model_reads_in_its_label_with_its_tiers(experio):
    unpointed(experio, payment_ref="PRLV FRAIS PACK GLOBAL BUSINESS")
    experio["operation_partner"] = {"partner_id": {"id": 31, "display_name": "BANQUE"}}
    experio["answer"] = {"lines": [{"id": 2, "operation": 13156, "payment_method": 3}]}
    message = pointed()["message"]
    assert ([2], {"operation_id": 13156, "partner_id": 31}) in experio["written"]  # no payment method: not a purchase
    assert "1 reçoivent de Tarik leur type d'opération" in message
    asked = experio["asked"][0]
    assert "13136 | Achat Local | purchase | outbound" in asked and "2 | 2026-03-15 | -200.00 | outbound | ? |" in asked


@pytest.mark.functionality
def test_a_purchase_line_gets_its_payment_method_with_its_operation(experio):
    unpointed(experio)
    experio["answer"] = {"lines": [{"id": 2, "operation": 13136, "payment_method": 2}]}
    pointed()
    assert ([2], {"operation_id": 13136, "exp_payment_method_id": 2}) in experio["written"]


@pytest.mark.functionality
def test_a_purchase_line_with_its_operation_gets_only_the_payment_method_it_lacks(experio):
    experio["lines"][1]["exp_payment_method_id"] = False
    experio["answer"] = {"lines": [{"id": 2, "operation": 13131, "payment_method": 2}]}
    pointed()
    assert ([2], {"exp_payment_method_id": 2}) in experio["written"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "choice",
    [
        {"id": 2, "operation": 13131, "payment_method": None},  # a sale's way, for money paid
        {"id": 2, "operation": 99999, "payment_method": None},  # not the dossier's
        {"id": 2, "operation": None, "payment_method": None},  # the label does not say
        {"id": 77, "operation": 13136, "payment_method": 2},  # no such line
        "13136",
    ],
)
def test_an_operation_the_line_cannot_take_is_never_written(experio, choice):
    unpointed(experio)
    experio["answer"] = {"lines": [choice]}
    pointed()
    assert not [w for w in experio["written"] if "operation_id" in w[1] or "exp_payment_method_id" in w[1]]


@pytest.mark.functionality
def test_without_the_model_the_lines_stay_for_the_cabinet_and_the_pointing_goes_on(experio):
    unpointed(experio)
    experio["answer"] = ToolError("Pointage du relevé : le modele n'est pas encore configure.")
    message = pointed()["message"]
    assert "2 pointée(s) par la détection d'Experio" in message and "type d'opération" not in message


@pytest.mark.functionality
def test_nothing_is_asked_of_the_model_when_every_line_has_its_operation(experio):
    pointed()
    assert not [a for a in experio["asked"] if a.startswith("Operations")]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("label", "given"),
    [
        ("VIREMENT EMIS STE MECAERIC SARLAU", 51),  # its name, legal forms aside
        ("VIR 001234567000089 REGLEMENT", 52),  # its ICE
        ("VIR MECAERIC ET ATLAS NEGOCE", None),  # two named: none
        ("VIR INST RECU PAR AUTRE BANQUE", None),  # none named
        ("VIR MECAERICA", None),  # not a whole word
    ],
)
def test_a_line_gets_the_one_tiers_its_label_names(experio, label, given):
    experio["lines"][1]["payment_ref"] = label
    experio["partners"] = [
        {"id": 51, "name": "MECAERIC SARL - 4471", "ice": False},
        {"id": 52, "name": "ATLAS NEGOCE", "ice": "001234567000089"},
        {"id": 53, "name": "SA", "ice": "000000000000000"},  # too short to tell, and no ICE
    ]
    pointed()
    written = [values["partner_id"] for line, values in experio["written"] if line == [2] and "partner_id" in values]
    assert written == ([given] if given else [])


@pytest.mark.functionality
def test_a_tiers_key_leaves_out_legal_forms_and_too_short_names():
    assert statement.tiers_key("STE ATLAS SARL - 12345") == "atlas"
    assert statement.tiers_key("SA") == "" and statement.tiers_key("Sté ABC") == ""


@pytest.mark.functionality
def test_a_line_with_its_tiers_gets_the_one_open_invoice_of_its_amount(experio):
    experio["owed"] = [70]
    message = pointed()["message"]
    assert ([1], {"invoice_ids": [(4, 70)]}) in experio["written"]
    assert experio["owing"][0][-1] == ("amount_residual", "=", 200.0)  # in the line's own picker
    assert "1 la seule facture ouverte de leur tiers et de leur montant" in message


@pytest.mark.functionality
@pytest.mark.parametrize("case", ["two", "in_use", "refused"])
def test_no_invoice_is_given_when_two_fit_one_is_used_or_the_form_refuses_it(experio, case):
    experio["owed"] = [70, 71] if case == "two" else [70]
    if case == "in_use":
        experio["in_use"] = [70]
    if case == "refused":
        experio["form"][70] = ExperioRefusal(SAME_ACCOUNT, "odoo.exceptions.UserError")
    pointed()
    assert not [w for w in experio["written"] if w[0] == [1]]


@pytest.mark.functionality
def test_a_dossiers_open_statements_are_listed_to_be_pointed_and_each_posted(experio):
    experio["open"] = [40, 41]
    listed = statement.list_open_statements(857)
    assert listed["statement_ids"] == [40, 41] and listed.get("routine") and "n° 40, n° 41" in listed["message"]
    posted = statement.post_dossier_statements(857)
    assert posted["message"].count("opération(s) comptabilisée(s)") == 2 and "\n- Relevé n° 41 :" in posted["message"]


@pytest.mark.functionality
def test_a_dossier_without_open_statement_says_so(experio):
    experio["open"] = []
    listed = statement.list_open_statements(857)
    assert listed["statement_ids"] == [] and "aucun relevé bancaire" in listed["message"]


@pytest.mark.functionality
def test_a_bank_statement_read_by_experio_goes_on_to_its_pointing_steps(experio, db_cabinet):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (id, session_id, file_name, mime_type, data) values (8, 's', 'r.pdf', 'x', 'x')"
        )
    result = statement.settle_statement(8)
    assert result["statement_id"] == 40 and result["bank"] and result.get("routine") and result["post_asked"] is False
    assert experio["applied"] == [] and experio["written"] == []  # its pointing is the next steps'


@pytest.mark.communication
def test_each_pointing_step_says_what_it_did_for_the_report(experio):
    detected = statement.detect_statement_lines(40)
    assert detected["said"] == "2 pointée(s) par la détection d'Experio" and detected.get("routine")
    assert statement.name_line_tiers(40)["message"] == "Relevé n° 40 : rien de plus."


@pytest.mark.functionality
def test_a_line_the_model_reads_two_ways_is_left_for_the_cabinet(experio):
    unpointed(experio)
    experio["lines"][2].update(operation_id=False, operation_type=False, exp_payment_method_id=False)
    same = {"id": 3, "operation": 13136, "payment_method": 2}
    experio["answer"] = (
        {"lines": [{"id": 2, "operation": 13136, "payment_method": 2}, same]},
        {"lines": [{"id": 2, "operation": 13156, "payment_method": None}, same]},
    )
    pointed()
    assert len(experio["asked"]) == 2
    written = [line for line, values in experio["written"] if "operation_id" in values]
    assert written == [[3]]


@pytest.mark.functionality
def test_a_purchase_line_gets_the_payment_method_its_label_says_when_the_model_says_none(experio):
    unpointed(experio, payment_ref="CHQ 1234 STE ATLAS")
    experio["answer"] = {"lines": [{"id": 2, "operation": 13136, "payment_method": None}]}
    pointed()
    assert ([2], {"operation_id": 13136, "exp_payment_method_id": 2}) in experio["written"]  # CHQ: Chèque


@pytest.mark.functionality
def test_a_purchase_line_with_no_agreed_answer_still_gets_the_method_its_label_says(experio):
    experio["lines"][1].update(exp_payment_method_id=False, payment_ref="VIR EMIS ATLAS")
    pointed()
    assert ([2], {"exp_payment_method_id": 4}) in experio["written"]  # VIR: Virement Bancaire, from the label alone


@pytest.mark.functionality
def test_a_label_saying_two_ways_or_none_gives_no_payment_method():
    methods = {2: "Chèque", 4: "Virement Bancaire"}
    assert (
        statement.method_in_label("CHQ 12 SUR VIR", methods) is None
        and statement.method_in_label("AGIOS", methods) is None
    )
    assert statement.method_in_label("VIREMENT ATLAS", methods) == 4


def partners_to_name(experio):
    experio["partners"] = [
        {"id": 51, "name": "MECAERIC SARL - 4471", "ice": False},
        {"id": 52, "name": "ATLAS", "ice": False},
    ]
    experio["lines"][1]["payment_ref"] = "VIR MECA"  # an abbreviation: no whole name in it


@pytest.mark.functionality
def test_a_tiers_no_name_matches_is_read_by_the_model_when_it_says_the_same_twice(experio):
    partners_to_name(experio)
    experio["answer"] = {"lines": [{"id": 2, "tiers": 51}]}
    pointed()
    assert ([2], {"partner_id": 51}) in experio["written"]
    assert "51 | MECAERIC SARL - 4471" in experio["asked"][0] and len(experio["asked"]) == 2


@pytest.mark.functionality
@pytest.mark.parametrize("answer", [{"lines": [{"id": 2, "tiers": 99}]}, {"lines": [{"id": 2, "tiers": None}]}])
def test_a_tiers_the_line_does_not_offer_is_never_written(experio, answer):
    partners_to_name(experio)
    experio["answer"] = answer
    pointed()
    assert not [w for w in experio["written"] if "partner_id" in w[1]]


@pytest.mark.security
def test_a_model_answer_with_a_list_for_an_id_is_ignored_not_a_crash(experio):
    partners_to_name(experio)
    experio["answer"] = {"lines": [{"id": [2], "tiers": 51}, {"id": 2, "tiers": [51]}]}
    pointed()
    assert not [w for w in experio["written"] if "partner_id" in w[1]]


@pytest.mark.functionality
def test_a_dossier_without_its_own_operations_is_told_to_name_a_model_dossier(experio):
    unpointed(experio)
    experio["operations"] = []
    assert "dossier modèle" in pointed()["message"]


@pytest.mark.security
def test_a_posting_date_far_from_today_leaves_the_fiscal_year_to_the_cabinet(experio, monkeypatch):
    from engine.tools.invoices import exercice

    experio["post"] = {"res_model": exercice.FISCAL_YEAR, "res_id": 6, "name": "Créer l'exercice fiscal"}
    monkeypatch.setattr(exercice, "odoo_json", lambda *a, **k: pytest.fail("no year opened for a misread date"))
    monkeypatch.setattr(exercice, "odoo_call", lambda *a, **k: [{"move_date": "2062-03-01"}])
    assert "« Créer l'exercice fiscal »" in statement.post_statement(8)["message"]


@pytest.mark.functionality
def test_a_new_fiscal_year_asked_at_posting_is_created_and_the_posting_goes_on(experio, monkeypatch):
    from engine.tools.invoices import exercice

    experio["post"] = {"res_model": exercice.FISCAL_YEAR, "res_id": 6, "name": "Créer l'exercice fiscal"}
    confirmed = []

    def confirm(model, method, ids, **kw):
        confirmed.append((model, method, ids))
        return True

    monkeypatch.setattr(exercice, "odoo_json", confirm)
    wizard = {"move_date": f"{date.today().year}-08-31", "date_start": "2026-01-01"}
    monkeypatch.setattr(exercice, "odoo_call", lambda *a, **k: [wizard])
    result = statement.post_statement(8)
    assert confirmed == [(exercice.FISCAL_YEAR, "action_create_and_post", [6])]
    assert result["status"] == "ok" and "écran" not in result["message"]


@pytest.mark.functionality
def test_a_dossier_with_no_exercice_gets_the_calendar_year_of_the_posting_date(experio, monkeypatch):
    from engine.tools.invoices import exercice

    experio["post"] = {"res_model": exercice.FISCAL_YEAR, "res_id": 6, "name": "Créer le premier exercice fiscal"}
    written = []
    year = date.today().year

    def odoo_call(model, method, *args, **kw):
        if method == "write":
            written.append(args)
        return [{"move_date": f"{year}-08-31", "date_start": False}]

    monkeypatch.setattr(exercice, "odoo_call", odoo_call)
    monkeypatch.setattr(exercice, "odoo_json", lambda *a, **k: True)
    assert statement.post_statement(8)["status"] == "ok"
    assert written == [([6], {"date_start": f"{year}-01-01", "date_end": f"{year}-12-31"})]


@pytest.mark.functionality
def test_a_line_the_model_gives_two_tiers_is_left_for_the_cabinet(experio):
    partners_to_name(experio)
    experio["answer"] = {"lines": [{"id": 2, "tiers": 51}, {"id": 2, "tiers": 52}]}
    pointed()
    assert not [w for w in experio["written"] if "partner_id" in w[1]]


@pytest.mark.functionality
def test_without_the_model_a_purchase_line_still_gets_the_method_its_label_says(experio):
    experio["lines"][1].update(exp_payment_method_id=False, payment_ref="CHQ 1234 ATLAS")
    unpointed_line = experio["lines"][0]
    unpointed_line["operation_id"] = False  # something for the model to be asked: it is down
    experio["answer"] = ToolError("Pointage du relevé : le modele n'est pas encore configure.")
    pointed()
    assert ([2], {"exp_payment_method_id": 2}) in experio["written"]


SUSPENSE = [  # the dossier's « comptes transitoires ou d'attente »: débiteurs (3497), créditeurs (4497)
    {"id": 13326, "name": "Transitoire - débiteurs", "type": False, "payment_type": False, "bank_type": False},
    {"id": 13355, "name": "Transitoire - créditeurs", "type": False, "payment_type": False, "bank_type": False},
]
SUSPENSE[0]["account_id"], SUSPENSE[1]["account_id"] = [8, "349700000 CPTS TRA"], [9, "449700000 CPTS TRA"]


def with_suspense(experio, **line):
    unpointed(experio, **line)
    for operation in SUSPENSE:
        operation["error_msg"] = False
    experio["operations"] += [dict(operation) for operation in SUSPENSE]
    experio["answer"] = {"lines": [{"id": 2, "operation": None, "payment_method": None}]}


@pytest.mark.functionality
@pytest.mark.parametrize(("debit", "credit", "operation"), [(200.0, 0.0, 13326), (0.0, 150.0, 13355)])
def test_a_line_nothing_says_goes_to_the_suspense_of_its_way_and_the_client_is_told(experio, debit, credit, operation):
    with_suspense(experio, payment_ref="CD AP 000155 030826", debit=debit, credit=credit)
    message = pointed()["message"]
    assert ([2], {"operation_id": operation}) in experio["written"]
    assert "1 ligne(s) sans indice dans le libellé mise(s) en compte d'attente (3497 ou 4497)" in message


@pytest.mark.security
def test_without_the_model_nothing_goes_to_suspense(experio):
    with_suspense(experio, payment_ref="CD AP 000155 030826")
    experio["answer"] = ToolError("Pointage du relevé : le modele n'est pas encore configure.")
    pointed()
    assert not [w for w in experio["written"] if "operation_id" in w[1]]


@pytest.mark.functionality
def test_the_card_insurance_is_a_bank_charge_without_tva(experio):
    with_suspense(experio, payment_ref="ASSURANCE SECURICARTE")
    experio["operations"] += [
        {
            "id": 5001,
            "name": "Frais (TTC)",
            "type": "bank_fee",
            "payment_type": "outbound",
            "bank_type": "frais_with_tax",
        },
        {
            "id": 5002,
            "name": "Frais (HT)",
            "type": "bank_fee",
            "payment_type": "outbound",
            "bank_type": "frais_without_tax",
        },
    ]
    for operation in experio["operations"][-2:]:
        operation.update(account_id=[6, "614730000 FRAIS ET COMMIS"], error_msg=False)
    pointed()
    assert ([2], {"operation_id": 5002}) in experio["written"]


@pytest.mark.functionality
@pytest.mark.parametrize(("declared", "operation"), [(1, 5003), (0, 13326)])
def test_a_dgi_payment_is_the_tva_due_only_when_a_declaration_owes_that_amount(experio, declared, operation):
    with_suspense(experio, payment_ref="PRELEVEMENT EN FAV. DGI")
    experio["operations"].append(
        {"id": 5003, "name": "TVA Due", "type": False, "payment_type": "outbound", "bank_type": False}
        | {"account_id": [7, "445600000 ETAT TVA DUE"], "error_msg": False}
    )
    experio["declared"] = declared
    pointed()
    assert ([2], {"operation_id": operation}) in experio["written"]  # else: suspense, the tax is the cabinet's


@pytest.mark.functionality
def test_a_cheque_line_without_tiers_gets_the_invoice_owing_its_amount_and_through_it_its_tiers(experio):
    experio["lines"][0]["partner_id"] = False
    experio["owed"] = [70]
    pointed()
    assert ([1], {"invoice_ids": [(4, 70)], "partner_id": 9}) in experio["written"]


@pytest.mark.functionality
def test_home_banking_in_a_label_is_pointed_autres_as_the_cabinets_do():
    methods = {2: "Chèque", 4: "Virement Bancaire", 6: "Autres"}
    assert statement.method_in_label("PAIEMENT LYDEC HB 06/08/2026", methods) == 6


@pytest.mark.functionality
def test_a_line_with_no_tiers_gets_the_tiers_when_all_invoices_of_its_amount_are_one_suppliers(experio):
    experio["owed"] = [70, 71]  # two equal invoices of ATLAS: no invoice is chosen, the supplier is known
    pointed()
    assert ([2], {"partner_id": 9}) in experio["written"]
    assert not [w for w in experio["written"] if w[0] == [2] and "invoice_ids" in w[1]]


@pytest.mark.functionality
def test_invoices_of_two_suppliers_give_no_tiers(experio):
    experio["owed"] = [70, 71]
    experio["owner"] = {71: [10, "BETA"]}
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "partner_id" in w[1]]


@pytest.mark.functionality
@pytest.mark.parametrize("generic", [[31], []])
def test_a_card_remittance_is_a_sale_on_clients_divers_or_goes_to_suspense(experio, generic):
    """The cabinets' own reading of « CD AP » credits: Vente Local on CLIENTS DIVERS (79 of 101 lines)."""
    with_suspense(experio, payment_ref="CD AP : 000155 030826", debit=0.0, credit=150.0)
    experio["generic"] = generic
    pointed()  # the dossier's Vente Local (13131, account 3421) is the sale
    expected = {"operation_id": 13131, "partner_id": 31} if generic else {"operation_id": 13355}
    assert ([2], expected) in experio["written"]  # no CLIENTS DIVERS in the dossier: suspense, as before


def cheque(experio, ref="PAIEMENT CHEQUE N 0373244"):
    experio["lines"][1]["payment_ref"] = ref
    return experio


@pytest.mark.functionality
def test_a_transfer_number_quoted_in_another_entry_names_the_supplier_and_the_cabinet_is_told(experio):
    cheque(experio, "VIR EMIS WEB 123456789")["by_number"] = [{"partner_id": [9, "ATLAS"], "debit": 0, "credit": 5.0}]
    message = pointed()["message"]
    assert ([2], {"partner_id": 9}) in experio["written"]  # a transfer's number alone: 62 of 65 right
    assert "leur tiers retrouvé dans le grand livre" in message and "à vérifier par le cabinet" in message


@pytest.mark.functionality
def test_a_cheque_number_counts_only_on_an_entry_of_the_lines_own_amount(experio):
    cheque(experio)["by_number"] = [{"partner_id": [9, "ATLAS"], "debit": 0, "credit": 999.0}]  # not the 200.00
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "partner_id" in w[1]]
    experio["by_number"] = [{"partner_id": [9, "ATLAS"], "debit": 200.0, "credit": 0}]
    pointed()
    assert ([2], {"partner_id": 9}) in experio["written"]


@pytest.mark.functionality
def test_an_unpaid_supplier_entry_of_the_exact_amount_names_the_supplier(experio):
    cheque(experio)["by_amount"] = [{"partner_id": [9, "ATLAS"]}]
    pointed()
    assert ([2], {"partner_id": 9}) in experio["written"]


@pytest.mark.functionality
def test_the_number_decides_before_the_amount(experio):
    cheque(experio, "VIR EMIS WEB 123456789")
    experio["by_number"], experio["by_amount"] = [{"partner_id": [9, "ATLAS"]}], [{"partner_id": [10, "BETA"]}]
    pointed()
    assert ([2], {"partner_id": 9}) in experio["written"] and ([2], {"partner_id": 10}) not in experio["written"]


@pytest.mark.functionality
@pytest.mark.parametrize("case", ["two suppliers", "refused by the picker", "nothing"])
def test_the_ledger_gives_no_tiers_when_it_names_two_or_the_line_refuses_it(experio, case):
    cheque(experio)
    if case == "two suppliers":
        experio["by_amount"] = [{"partner_id": [9, "ATLAS"]}, {"partner_id": [10, "BETA"]}]
    if case == "refused by the picker":
        experio["by_amount"], experio["accepts"] = [{"partner_id": [9, "ATLAS"]}], False
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "partner_id" in w[1]]


def labelled(experio, ref, *numbers):
    experio["lines"][1]["payment_ref"] = ref
    experio["labelled"] = [{"id": 70, "invoice_number": n} for n in numbers]
    return experio


@pytest.mark.functionality
def test_an_invoice_number_in_the_label_ties_the_invoice_and_gives_the_tiers(experio):
    labelled(experio, "CHQ N° 0902108_PAP BAT_F25-111001", "F25-111001")
    experio["lines"][1]["partner_id"] = False
    message = pointed()["message"]
    assert ([2], {"invoice_ids": [(4, 70)], "partner_id": 9}) in experio["written"]
    assert "rattachée(s) à la facture dont le numéro est dans le libellé" in message


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("ref", "numbers"),
    [
        ("PAIEMENT CHEQUE N 0373244", ("0373244999",)),  # a longer number holding the label's: not the same
        ("VIR 13/02/2025 SANS NUMERO", ("2025",)),  # a date is no number
        ("CHQ N° 0902108", ("F25-111001",)),  # another invoice
    ],
)
def test_only_an_invoice_whose_number_is_exactly_the_labels_is_tied(experio, ref, numbers):
    labelled(experio, ref, *numbers)
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "invoice_ids" in w[1]]


@pytest.mark.functionality
def test_two_invoices_with_the_labels_number_tie_none(experio):
    labelled(experio, "VIREMENT_MEFELEC_29457", "29457")
    experio["labelled"].append({"id": 71, "invoice_number": "0029457"})  # the same number once zeros are dropped
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "invoice_ids" in w[1]]


def past(label, operation=None, partner=None, method=None):
    """A line the dossier's cabinet posted before."""
    return {
        "payment_ref": label,
        "operation_id": operation and [operation, "op"],
        "partner_id": partner and [partner, "tiers"],
        "exp_payment_method_id": method and [method, "méthode"],
    }


@pytest.mark.functionality
def test_a_label_the_dossier_always_pointed_one_way_gets_that_operation_without_asking_the_model(experio):
    unpointed(experio, payment_ref="PRLV FRAIS PACK GLOBAL BUSINESS 0456")
    experio["history"] = [
        past("PRLV FRAIS PACK GLOBAL BUSINESS 0123", 13156),
        past("PRLV frais pack global business", 13156),
    ]
    pointed()
    assert any(w[0] == [2] and w[1].get("operation_id") == 13156 for w in experio["written"])
    assert not [a for a in experio["asked"] if a.startswith("Operations")]  # the memory settled it


@pytest.mark.functionality
def test_a_label_the_dossier_pointed_two_ways_is_left_to_the_model(experio):
    unpointed(experio, payment_ref="PRLV FRAIS PACK GLOBAL BUSINESS 0456")
    experio["history"] = [
        past("PRLV FRAIS PACK GLOBAL BUSINESS", 13156),
        past("PRLV FRAIS PACK GLOBAL BUSINESS", 13131),
    ]
    experio["answer"] = {"lines": []}
    pointed()
    assert not [w for w in experio["written"] if w[0] == [2] and "operation_id" in w[1]]
    assert experio["asked"]  # not unanimous: the model is asked


@pytest.mark.functionality
def test_a_purchase_label_the_dossier_always_paid_one_way_gets_that_payment_method(experio):
    experio["lines"][1].update(exp_payment_method_id=False, payment_ref="PAIEMENT DE VOTRE FACTURE 0912")
    experio["history"] = [past("PAIEMENT DE VOTRE FACTURE 0001", 13136, method=2)]
    pointed()
    assert ([2], {"exp_payment_method_id": 2}) in experio["written"]
    assert not [a for a in experio["asked"] if a.startswith("Operations")]


@pytest.mark.functionality
@pytest.mark.parametrize(("seen", "written"), [(2, True), (1, False)])
def test_a_tiers_the_dossier_always_gave_a_label_is_given_only_when_seen_twice(experio, seen, written):
    experio["partners"] = [{"id": 51, "name": "MECAERIC SARL - 4471", "ice": False}]
    experio["lines"][1]["payment_ref"] = "VIR EMIS WEB 0912"  # no name: only the memory knows
    experio["history"] = [past(f"VIR EMIS WEB {n}", 13136, partner=51) for n in range(seen)]
    pointed()
    assert (([2], {"partner_id": 51}) in experio["written"]) is written


@pytest.mark.functionality
def test_a_request_to_post_made_with_the_statement_is_kept_for_the_run_resumed_after_a_question(experio, db_cabinet):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (id, session_id, file_name, mime_type, data) values (8, 's', 'r.pdf', 'x', 'x')"
        )
        db.execute(Q.mark_file_post_asked, (8,))  # what import_bank_statement does for comptabiliser « oui »
    assert statement.settle_statement(8)["post_asked"] is True


def named_in_label(experio, monkeypatch, ref, name, lines=(2,)):
    monkeypatch.setattr(statement, "series_account", lambda partner, kind, context: "441100999")
    for line_id in lines:
        experio["lines"][line_id - 1]["payment_ref"] = ref
    experio["answer"] = {"lines": [{"id": i, "tiers": None, "name": name} for i in lines]}
    return experio


@pytest.mark.functionality
def test_a_counterparty_the_dossier_does_not_know_is_created_from_the_label_and_told(experio, monkeypatch):
    named_in_label(experio, monkeypatch, "VIR EMIS WEB SOCIETE ATLAS NEGOCE 0912", "ATLAS NEGOCE")
    message = pointed()["message"]
    assert experio["created"] == [{"name": "ATLAS NEGOCE", "type": "company", "company_id": 857, "is_supplier": True}]
    assert ([2], {"partner_id": 77}) in experio["written"]
    assert ([77], {"third_party_supplier_account": "441100999"}) in experio["created_written"]  # the series' code
    assert "Tiers créé(s) d'après le libellé : `ATLAS NEGOCE`" in message


@pytest.mark.functionality
def test_a_sale_line_gets_a_customer_and_one_tiers_serves_every_line_naming_it(experio, monkeypatch):
    for line in experio["lines"][1:]:
        line["operation_type"] = "sale"
    named_in_label(experio, monkeypatch, "VIR RECU DE KAYSER CONCEPT", "KAYSER CONCEPT", lines=(2, 3))
    pointed()
    assert len(experio["created"]) == 1 and experio["created"][0]["is_customer"] is True
    assert ([2], {"partner_id": 77}) in experio["written"] and ([3], {"partner_id": 77}) in experio["written"]


@pytest.mark.security
@pytest.mark.parametrize("name", ["ATLAS NEGOCE SARL AU", "AT", "123456", None])
def test_a_name_the_label_does_not_carry_creates_no_tiers(experio, monkeypatch, name):
    named_in_label(experio, monkeypatch, "VIR EMIS WEB SOCIETE ATLAS NEGOCE 0912", name)
    pointed()
    assert experio["created"] == []
