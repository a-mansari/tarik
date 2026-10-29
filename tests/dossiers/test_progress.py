"""Where a dossier stands: Experio's counts by its own statuses, its « avancement », every exercice's rows counted."""

import pytest

from engine.tools.dossiers import progress

EXERCICE = {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}
GROUPS = {
    ("exp.invoice", ("type", "state")): [
        {"type": "purchase", "state": "draft", "__count": 3},
        {"type": "purchase", "state": "post", "__count": 5},
        {"type": "sale", "state": "export", "__count": 2},
    ],
    ("exp.invoice", ("type", "payment_state")): [{"type": "purchase", "payment_state": "paid", "__count": 5}],
    ("exp.bank.statement", ("state",)): [{"state": "partial", "__count": 1}],
    ("exp.bank.statement.line", ("state",)): [{"state": "draft", "__count": 2}, {"state": "post", "__count": 8}],
    ("exp.monetary.statement", ("state",)): [],
    ("exp.monetary.statement.line", ("state",)): [],
}


@pytest.fixture
def experio(monkeypatch):
    """Dossier BETA's counts; the contexts read_group was called with."""
    contexts = []
    monkeypatch.setattr(progress, "require_dossier", lambda dossier_id: "BETA")

    def odoo_call(model, method, *args, **kw):
        if method == "read_group":
            contexts.append(kw["context"])
            return GROUPS[(model, tuple(args[2]))]
        if model == "exp.fiscal.entries":
            return [EXERCICE]
        if method == "search_count" and model == "exp.bank.statement.line" and len(args[0]) > 2:
            return 4  # the suspense
        if method == "search_count":  # Experio's progress: per model, booked then total
            return {"exp.invoice": 7, "exp.receipt": 1, "exp.bank.statement.line": 8}.get(model, 0) * (
                1 if ("state", "!=", "cancel") not in args[0] else 2
            )
        if model == "exp.deduction.table":
            return [{"state": "done", "period_id": [1, "Trimestre 1"]}, {"state": "draft", "period_id": [2, "T2"]}]
        if model == "exp.payment.delay":
            raise RuntimeError("Object exp.payment.delay doesn't exist")  # not installed: left out
        return []

    monkeypatch.setattr(progress, "odoo_call", odoo_call)
    return contexts


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
    assert all(context["skip_fiscal_year_filter"] for context in experio)  # every exercice's posted rows counted


@pytest.mark.functionality
def test_a_routine_reads_the_same_progress_as_figures_for_its_table(experio):
    result = progress.dossier_progress_figures(5)
    assert result["message"] == progress.dossier_progress(5)
    figures = {"dossier": "BETA", "progress": 50, "pieces_booked": 8, "pieces": 16, "operations_booked": 8}
    assert figures.items() <= result["figures"].items()
    assert result["figures"]["suspense"] == 4 and result["figures"]["exercice"] == "Exercice 2026"
