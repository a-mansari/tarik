"""Where a dossier stands: Experio's counts by its own statuses, its « avancement », every exercice's rows counted;
many dossiers read at once as each alone."""

import operator

import pytest

from engine.tools.core.app import ToolError
from engine.tools.dossiers import progress

NAMES = {5: "BETA", 6: "GAMMA", 7: "DELTA"}
OPS = {"=": operator.eq, "!=": operator.ne, "in": lambda a, b: a in b, ">=": operator.ge, "<=": operator.le}


def rows(n, **fields):
    return [dict(fields) for _ in range(n)]


def records():
    """Experio's records: BETA (5) with exercices 2025 and 2026, GAMMA (6) with 2025 only, DELTA (7) with none."""
    exercice = {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}
    beta_invoice = {"company_id": 5, "exercice_id": 1, "invoice_date": "2026-03-01", "payment_state": "not_paid"}
    return {
        "exp.fiscal.entries": [
            {"company_id": 5, **exercice},
            {"company_id": 5, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"},
            {"company_id": 6, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"},
        ],
        "exp.invoice": [
            *rows(3, **beta_invoice, type="purchase", state="draft"),
            *rows(5, **{**beta_invoice, "payment_state": "paid"}, type="purchase", state="post"),
            *rows(2, **beta_invoice, type="sale", state="export"),
            *rows(1, **{**beta_invoice, "invoice_date": "2025-05-01"}, type="sale", state="post"),  # 2025's
            *rows(1, **{**beta_invoice, "state": "cancel"}, type="sale"),
            *rows(
                2,
                company_id=6,
                exercice_id=False,
                invoice_date="",
                type="purchase",
                state="draft",
                payment_state="paid",
            ),  # undated
            *rows(1, company_id=6, exercice_id=2, invoice_date="2026-02-01", type="sale", state="post"),  # 2026's
            *rows(
                1,
                company_id=6,
                exercice_id=2,
                invoice_date="2025-02-01",
                type="sale",
                state="post",
                payment_state="paid",
            ),
            *rows(4, company_id=7, exercice_id=False, invoice_date="2024-02-01", type="purchase", state="export"),
        ],
        "exp.receipt": rows(5, company_id=5, state="draft"),
        "exp.bank.statement": [
            {"company_id": 5, "state": "partial", "date": "2026-02-28"},
            {"company_id": 6, "state": "post", "date": "2025-02-28"},
            {"company_id": 6, "state": "draft", "date": "2026-02-28"},
        ],
        "exp.bank.statement.line": [
            *rows(2, company_id=5, state="draft", date="2026-02-01", exp_deleted=False),
            *rows(4, company_id=5, state="post", date="2026-02-01", exp_deleted=False, payment_state="paid"),
            *rows(4, company_id=5, state="post", date="2026-02-01", exp_deleted=False, payment_state="unpaid"),
            *rows(1, company_id=5, state="draft", date="2026-02-01", exp_deleted=True),  # deleted: no operation of its
            *rows(3, company_id=6, state="export", date="2025-02-01", exp_deleted=False, payment_state="partial"),
            *rows(2, company_id=7, state="draft", date="2024-02-01", exp_deleted=False),
        ],
        "exp.cash.statement.line": rows(5, company_id=5, state="draft"),
        "exp.monetary.statement": [{"company_id": 6, "state": "ready", "date": "2025-04-01"}],
        "exp.monetary.statement.line": rows(2, company_id=6, state="post", date="2025-04-01"),
        "exp.deduction.table": [
            {"company_id": 5, "fiscal_year": "2026", "state": "draft", "period_id": [2, "T2"], "start_date": "2026-04"},
            {"company_id": 5, "fiscal_year": "2026", "state": "done", "period_id": [1, "Trimestre 1"]},
            {"company_id": 5, "fiscal_year": "2025", "state": "done", "period_id": [9, "T4 2025"]},
            {"company_id": 6, "fiscal_year": "2025", "state": "done", "period_id": False, "start_date": "2025"},
            {"company_id": 6, "fiscal_year": "2026", "state": "draft", "period_id": [3, "T1 2026"]},  # not its year
        ],
        "exp.withholding.table": [
            {"company_id": 6, "fiscal_year": "2025", "type": "ir", "state": "draft", "period_id": [4, "Mars"]},
            {"company_id": 6, "fiscal_year": "2025", "type": "is", "state": "cancel", "period_id": [5, "Avril"]},
        ],
    }


def matches(record, domain):
    """Whether ``record`` is in ``domain`` (Odoo's prefix notation)."""

    def term(i):
        if domain[i] in ("|", "&"):
            left, j = term(i + 1)
            right, j = term(j)
            return (left or right) if domain[i] == "|" else (left and right), j
        field, op, value = domain[i]
        return OPS[op](record.get(field, False), value), i + 1

    i, found = 0, True
    while i < len(domain):
        one, i = term(i)
        found = found and one
    return found


@pytest.fixture
def experio(monkeypatch):
    """Experio on records(), its declaration Délais de paiement not installed; the calls made with their contexts."""
    made = []
    data = records()
    monkeypatch.setattr(progress, "require_dossier", lambda dossier_id: NAMES[dossier_id])

    def shown(record):
        return {**record, "company_id": [record["company_id"], NAMES[record["company_id"]]]}

    def odoo_call(model, method, domain, *args, context, **kw):
        made.append((model, method, context))
        if model == "exp.payment.delay":
            raise RuntimeError("Object exp.payment.delay doesn't exist")  # not installed: left out
        assert {r["company_id"] for r in data.get(model, []) if matches(r, domain)} <= set(
            context["allowed_company_ids"]
        )
        found = [shown(r) for r in data.get(model, []) if matches(r, domain)]
        if method == "read_group":  # args: its fields, its groups
            groups = {}
            for record in found:
                key = tuple(str(record.get(name)) for name in args[1])
                groups.setdefault(key, {name: record.get(name) for name in args[1]} | {"__count": 0})
                groups[key]["__count"] += 1
            return list(groups.values())
        for name in reversed(kw["order"].split(", ")):  # stable sorts: the first field sorted last
            field, _, way = name.partition(" ")
            found.sort(key=lambda r: str(r.get(field, "")), reverse=way == "desc")
        return found

    monkeypatch.setattr(progress, "odoo_call", odoo_call)
    return made


@pytest.mark.communication
def test_a_dossiers_progress_by_experios_own_statuses(experio):
    text = progress.dossier_progress(5)
    assert text.split("\n\n")[:2] == [  # a blank line between blocks: the chat and the email keep them apart
        "Dossier `BETA`, avancement selon Experio (tous exercices) : 50 %\n"
        "- 8 pièces comptabilisées sur 16\n"
        "- 8 opérations bancaires et de caisse comptabilisées sur 16",
        "Exercice : Exercice 2026, du 01/01/2026 au 31/12/2026",
    ]
    assert (
        "- Factures d'achat : 8, dont 3 en brouillon et 5 comptabilisées\n  - Parmi les comptabilisées : 5 payées"
        in text
    )
    assert "- Factures de vente : 2, dont 2 exportées" in text
    assert (
        "- Relevés bancaires : 1, dont 1 partiel\n  - Leurs opérations : 10, dont 2 en brouillon et 8 comptabilisées"
        in text
    )
    assert "- Relevés TPE : 0" in text and "- Opérations bancaires en suspens, sans facture lettrée : 4" in text
    assert "Déclarations 2026 :\n- TVA : Trimestre 1 déclarée, T2 en brouillon" in text
    assert "Délais" not in text  # a declaration Experio lacks is left out
    groups = [context for _, method, context in experio if method == "read_group"]
    assert all(context["skip_fiscal_year_filter"] for context in groups)  # every exercice's posted rows counted


@pytest.mark.functionality
def test_a_routine_reads_the_same_progress_as_figures_for_its_table(experio):
    result = progress.dossier_progress_figures(5)
    assert result["message"] == progress.dossier_progress(5)
    figures = {"dossier": "BETA", "progress": 50, "pieces_booked": 8, "pieces": 16, "operations_booked": 8}
    assert figures.items() <= result["figures"].items()
    assert result["figures"]["suspense"] == 4 and result["figures"]["exercice"] == "Exercice 2026"


@pytest.mark.functionality
def test_many_dossiers_read_at_once_say_what_each_says_alone(experio):
    alone = {i: progress.progress_of(i) for i in NAMES}
    experio.clear()
    together = progress.progress_of_many([{"id": i, "name": name} for i, name in NAMES.items()])
    assert together == alone and len(experio) == 26  # the same calls as for one dossier
    gamma, delta = alone[6][0], alone[7][0]
    assert "Exercice 2025" in gamma and "- Factures d'achat : 2, dont 2 en brouillon" in gamma  # undated: Experio's
    assert "Déclarations 2025 :\n- TVA : 2025 déclarée\n- RAS IR : Mars en brouillon" in gamma  # its own year only
    assert "Relevés TPE : 1, dont 1 prêt" in gamma and "Déclarations" not in delta
    assert "Exercice : aucun dans Experio" in delta and "- Factures d'achat : 4, dont 4 exportées" in delta
    assert all(context["allowed_company_ids"] == [5, 6, 7] for *_, context in experio)  # one context, all three


@pytest.mark.functionality
def test_a_year_named_reads_its_exercice_or_says_there_is_none(experio):
    assert "Exercice : Exercice 2025" in progress.dossier_progress(5, 2025)
    assert "- Factures de vente : 1, dont 1 comptabilisée" in progress.dossier_progress(5, 2025)
    with pytest.raises(ToolError, match="pas d'exercice 2026"):
        progress.dossier_progress(6, 2026)
