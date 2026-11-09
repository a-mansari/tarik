"""The curated reads of a dossier's books: one gate, an exercice resolved as dossier_progress does, balances said as an
accountant says them, and the caveats that stop the agent from calling an unfinished exercice a result."""

import pytest

from engine import agent
from engine.tools.core.app import ToolError
from engine.tools.data import query, reports

DOSSIER = 5
EXERCICE = {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}


class Experio:
    """A fake Experio: the exercice, then rows to a group read, lines to a read, a count to a count."""

    def __init__(self, monkeypatch, groups=None, lines=None, opening=1, exercice=EXERCICE, refusal=""):
        self.calls = []
        self.groups, self.lines, self.opening = groups or [], lines or [], opening
        monkeypatch.setattr(query, "require_dossier", lambda dossier_id: "ALPHA")
        monkeypatch.setattr(query, "allowed", lambda session_id, dossier_id, name: refusal)
        monkeypatch.setattr(reports, "exercices_of", lambda ids, year, context: {DOSSIER: exercice} if exercice else {})
        monkeypatch.setattr(reports, "odoo_call", self.call)

    def call(self, model, method, domain, *more, **kw):
        self.calls.append((method, domain, more, kw))
        if method == "search_count":
            return self.opening if any(term[0] == "journal_id.journal_category" for term in domain) else len(self.lines)
        return self.groups if method == "read_group" else self.lines[: kw["limit"]]


def row(code, label, debit, credit, **more):
    return {
        "account_id": [1, f"{code} {label}"],
        "partner_id": [1, "ATLAS"],
        "date:month": "janvier 2026",
        "debit": debit,
        "credit": credit,
        **more,
    }


@pytest.fixture
def experio(monkeypatch):
    return Experio(monkeypatch, groups=[row("71110000", "Ventes", 0.0, 100.0), row("61110000", "Achats", 60.0, 5.0)])


@pytest.mark.security
def test_every_report_reads_inside_the_dossier_with_the_registry_base_and_context(experio):
    reports.trial_balance(DOSSIER, "s1", account_prefix="6")
    reports.monthly_totals(DOSSIER, "s1", "7")
    reports.partner_balances(DOSSIER, "s1", "4411")
    reports.account_ledger(DOSSIER, "s1", "6")
    for method, domain, _, kw in experio.calls:
        assert domain[0] == ("company_id", "=", DOSSIER)
        assert all(term in domain for term in query.MODELS["exp.move.line"].base)
        assert ("date", "<=", "2026-12-31") in domain
        assert kw["context"] == {"allowed_company_ids": [DOSSIER], "skip_fiscal_year_filter": True}
        assert method in {"read_group", "search_read", "search_count"}


@pytest.mark.security
def test_a_conversation_that_may_not_read_the_dossier_gets_no_report(monkeypatch):
    experio = Experio(monkeypatch, refusal="Pas votre dossier.")
    for report in (
        lambda: reports.trial_balance(DOSSIER, "s1"),
        lambda: reports.monthly_totals(DOSSIER, "s1", "7"),
        lambda: reports.partner_balances(DOSSIER, "s1", "4411"),
        lambda: reports.account_ledger(DOSSIER, "s1", "6"),
    ):
        with pytest.raises(ToolError, match="Pas votre dossier"):
            report()
    assert experio.calls == []


@pytest.mark.security
@pytest.mark.parametrize("prefix", ["6 or 1=1", "61%", "abc", "123456789", "6,7", "-1"])
def test_an_account_prefix_that_is_not_digits_is_refused(experio, prefix):
    with pytest.raises(ToolError, match="account_prefix"):
        reports.trial_balance(DOSSIER, "s1", account_prefix=prefix)
    assert experio.calls == []


@pytest.mark.functionality
def test_a_balance_is_said_debiteur_or_crediteur_with_totals(experio):
    text = reports.trial_balance(DOSSIER, "s1")
    assert "Dossier `ALPHA`, Exercice 2026 du 01/01/2026 au 31/12/2026 (ouvert)." in text
    assert "- 61110000 Achats : débit 60.00, crédit 5.00, solde débiteur 55.00" in text
    assert "- 71110000 Ventes : débit 0.00, crédit 100.00, solde créditeur 100.00" in text
    assert text.index("61110000") < text.index("71110000")  # by code
    assert "Total : débit 60.00, crédit 105.00, solde créditeur 45.00." in text


@pytest.mark.functionality
def test_an_exercice_not_closed_never_reads_as_a_result(experio):
    assert reports.OPEN_NOTE in reports.trial_balance(DOSSIER, "s1", account_prefix="7")
    assert reports.OPEN_NOTE not in reports.trial_balance(DOSSIER, "s1", account_prefix="4")


@pytest.mark.functionality
def test_a_closed_exercice_carries_no_open_note(monkeypatch):
    Experio(monkeypatch, groups=[row("71110000", "Ventes", 0.0, 1.0)], exercice={**EXERCICE, "state": "shut"})
    assert reports.OPEN_NOTE not in reports.trial_balance(DOSSIER, "s1")


@pytest.mark.functionality
def test_balance_sheet_accounts_without_opening_entries_say_so(monkeypatch):
    experio = Experio(monkeypatch, groups=[row("51410100", "Banque", 1.0, 3.0)], opening=0)
    assert reports.NO_OPENING_NOTE in reports.trial_balance(DOSSIER, "s1", account_prefix="5")
    assert reports.NO_OPENING_NOTE not in reports.trial_balance(DOSSIER, "s1", account_prefix="6")
    assert any(term[0] == "journal_id.journal_category" for _, domain, _, _ in experio.calls for term in domain)


@pytest.mark.communication
def test_a_dossier_without_the_exercice_asked_is_told_so(monkeypatch):
    Experio(monkeypatch, exercice=None)
    with pytest.raises(ToolError, match="n'a pas d'exercice 2019"):
        reports.trial_balance(DOSSIER, "s1", year=2019)
    for bad in (-1, "2026", True, 99999):
        with pytest.raises(ToolError, match="year"):
            reports.trial_balance(DOSSIER, "s1", year=bad)


@pytest.mark.functionality
def test_nothing_booked_is_said_plainly(monkeypatch):
    Experio(monkeypatch)
    assert "Aucune écriture comptabilisée" in reports.trial_balance(DOSSIER, "s1", account_prefix="7")
    assert "Aucun solde ouvert" in reports.partner_balances(DOSSIER, "s1", "4411")


@pytest.mark.functionality
def test_partner_balances_are_open_items_up_to_the_exercice_end_biggest_first(monkeypatch):
    groups = [
        {"partner_id": [1, "PETIT"], "debit": 10.0, "credit": 0.0},
        {"partner_id": [2, "GROS"], "debit": 0.0, "credit": 900.0},
        {"partner_id": False, "debit": 5.0, "credit": 0.0},
    ]
    experio = Experio(monkeypatch, groups=groups)
    text = reports.partner_balances(DOSSIER, "s1", "4411")
    assert text.index("GROS") < text.index("PETIT") < text.index("sans tiers")
    assert "- GROS : créditeur 900.00" in text
    assert "Total : créditeur 885.00." in text
    domain = experio.calls[0][1]
    assert ("reconciled", "=", False) in domain and not any(t[:2] == ("date", ">=") for t in domain)


@pytest.mark.functionality
def test_monthly_totals_list_each_month_booked(monkeypatch):
    groups = [{"date:month": "janvier 2026", "debit": 0.0, "credit": 10.0},
              {"date:month": "février 2026", "debit": 4.0, "credit": 0.0}]  # fmt: skip
    experio = Experio(monkeypatch, groups=groups)
    text = reports.monthly_totals(DOSSIER, "s1", "7")
    assert "- janvier 2026 : débit 0.00, crédit 10.00, solde créditeur 10.00" in text
    assert "- février 2026 : débit 4.00, crédit 0.00, solde débiteur 4.00" in text
    assert experio.calls[0][3]["orderby"] == "date:month"
    with pytest.raises(ToolError, match="account_prefix"):
        reports.monthly_totals(DOSSIER, "s1", "")


@pytest.mark.functionality
def test_the_ledger_shows_its_lines_and_what_it_leaves_out(monkeypatch):
    lines = [
        {"date": f"2026-02-0{n}", "journal_id": [1, "ACHAT"], "move_id": [n, f"FA{n}"], "name": "Achat",
         "partner_id": [7, "ATLAS"], "debit": 10.0, "credit": 0.0}
        for n in range(1, 4)
    ]  # fmt: skip
    experio = Experio(monkeypatch, lines=lines)
    text = reports.account_ledger(DOSSIER, "s1", "6", limit=2)
    assert "3 ligne(s)" in text and "01/02/2026 | ACHAT | FA1 | Achat | ATLAS | débit 10.00 | crédit 0.00" in text
    assert "1 ligne(s) de plus non affichées" in text
    assert experio.calls[0][3]["limit"] == 2
    reports.account_ledger(DOSSIER, "s1", "6", limit=10_000)
    assert experio.calls[-1][3]["limit"] == reports.MAX_LINES
    reports.account_ledger(DOSSIER, "s1", "6", partner_id=7)
    assert ("partner_id", "=", 7) in experio.calls[-1][1]
    for bad in ({"partner_id": "7"}, {"limit": True}, {"partner_id": -1}):
        with pytest.raises(ToolError, match="entier"):
            reports.account_ledger(DOSSIER, "s1", "6", **bad)


@pytest.mark.technical
def test_a_long_list_says_it_was_cut(monkeypatch):
    groups = [row(f"6{n:07d}", "Compte", 1.0, 0.0) for n in range(reports.MAX_GROUPS + 1)]
    Experio(monkeypatch, groups=groups)
    assert f"Liste coupée à {reports.MAX_GROUPS}" in reports.trial_balance(DOSSIER, "s1", account_prefix="6")


@pytest.mark.security
def test_the_agent_cannot_set_the_conversation_of_a_report():
    for name in ("trial_balance", "monthly_totals", "partner_balances", "account_ledger"):
        arguments = set(next(t for t in agent.TOOLS if t.name == f"Experio_{name}").args)
        assert "session_id" not in arguments and "dossier_id" in arguments, name
