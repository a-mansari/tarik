"""The curated reads of a dossier's books: one gate, an exercice resolved as dossier_progress does, balances said as an
accountant says them, and the caveats that stop the agent from calling an unfinished exercice a result."""

import asyncio
from types import SimpleNamespace

import pytest

from engine import agent
from engine.tools.core.app import ToolError
from engine.tools.data import query, reports

DOSSIER = 5
EXERCICE = {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}


class Experio:
    """A fake Experio: the exercice, then rows to a group read, lines to a read, a count to a count, and whether the
    exercice holds opening entries to a search."""

    def __init__(self, monkeypatch, groups=None, lines=None, opening=True, exercice=EXERCICE, refusal=""):
        self.calls = []
        self.groups, self.lines, self.opening = groups or [], lines or [], opening
        monkeypatch.setattr(query, "require_dossier", lambda dossier_id: "ALPHA")
        monkeypatch.setattr(query, "allowed", lambda session_id, dossier_id, name: refusal)
        monkeypatch.setattr(reports, "exercices_of", lambda ids, year, context: {DOSSIER: exercice} if exercice else {})
        monkeypatch.setattr(reports, "odoo_call", self.call)

    def call(self, model, method, domain, *more, **kw):
        self.calls.append((method, domain, more, kw))
        if method == "search":
            return [1] if self.opening else []
        if method == "search_count":
            return len(self.lines)
        return self.groups if method == "read_group" else self.lines[: kw["limit"]]


def row(code, label, debit, credit, **more):
    return {
        "account_id": [1, f"{code} {label}"],
        "partner_id": [1, "ATLAS"],
        "date:month": "janvier 2026",
        "debit": debit,
        "credit": credit,
        "amount_residual": debit - credit,
        **more,
    }


@pytest.fixture
def experio(monkeypatch):
    return Experio(monkeypatch, groups=[row("71110000", "Ventes", 0.0, 100.0), row("61110000", "Achats", 60.0, 5.0)])


@pytest.mark.security
def test_every_report_reads_inside_the_dossier_with_the_registry_base_and_context(monkeypatch):
    experio = Experio(monkeypatch, groups=[row("44110000", "Fournisseurs", 1.0, 2.0)], lines=[a_line(1)])
    reports.trial_balance(DOSSIER, "s1", account_prefix="4")
    reports.monthly_totals(DOSSIER, "s1", "4")
    reports.partner_balances(DOSSIER, "s1", "4411")
    reports.account_ledger(DOSSIER, "s1", "4", limit=1)
    assert {method for method, *_ in experio.calls} == {"read_group", "search_read", "search_count", "search"}
    for _, domain, _, kw in experio.calls:
        assert domain[0] == ("company_id", "=", DOSSIER)
        assert all(term in domain for term in query.MODELS["exp.move.line"].base)
        assert ("date", "<=", "2026-12-31") in domain
        assert kw["context"]["allowed_company_ids"] == [DOSSIER] and kw["context"]["skip_fiscal_year_filter"] is True


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
@pytest.mark.parametrize("prefix", ["6 or 1=1", "61%", "abc", "123456789", "6,7", "-1", "٣٤٢١"])
def test_an_account_prefix_that_is_not_ascii_digits_is_refused(experio, prefix):
    with pytest.raises(ToolError, match="account_prefix"):
        reports.trial_balance(DOSSIER, "s1", account_prefix=prefix)
    assert experio.calls == []


@pytest.mark.security
def test_a_third_party_report_needs_a_precise_prefix(experio):
    with pytest.raises(ToolError, match="au moins 3 chiffres"):
        reports.partner_balances(DOSSIER, "s1", "44")
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
def test_the_opening_entries_are_told_apart_by_their_category_as_experios_balance_does(monkeypatch):
    experio = Experio(monkeypatch, groups=[row("51410100", "Banque", 1.0, 3.0)], opening=False)
    assert reports.NO_OPENING_NOTE in reports.trial_balance(DOSSIER, "s1", account_prefix="5")
    experio.opening = True
    text = reports.trial_balance(DOSSIER, "s1", account_prefix="5")
    assert reports.OPENING_NOTE in text and reports.NO_OPENING_NOTE not in text
    method, domain, _, kw = next(call for call in experio.calls if call[0] == "search")
    assert ("move_id.move_category", "=", "carry_forward") in domain and kw["limit"] == 1
    assert not any(term[0] == "journal_id.journal_category" for term in domain)


@pytest.mark.technical
def test_the_result_accounts_never_ask_experio_about_opening_entries(experio):
    reports.trial_balance(DOSSIER, "s1", account_prefix="7")
    assert "search" not in {method for method, *_ in experio.calls}


@pytest.mark.communication
def test_a_dossier_without_the_exercice_asked_is_told_so(monkeypatch):
    Experio(monkeypatch, exercice=None)
    with pytest.raises(ToolError, match="n'a pas d'exercice 2019"):
        reports.trial_balance(DOSSIER, "s1", year=2019)
    for bad in (-1, "2026", True, 99999):
        with pytest.raises(ToolError, match="year"):
            reports.trial_balance(DOSSIER, "s1", year=bad)


@pytest.mark.technical
def test_an_exercice_in_a_state_not_known_still_has_a_heading(monkeypatch):
    Experio(monkeypatch, groups=[row("71110000", "Ventes", 0.0, 1.0)], exercice={**EXERCICE, "state": "autre"})
    assert "(autre)" in reports.trial_balance(DOSSIER, "s1")


@pytest.mark.functionality
def test_nothing_booked_is_said_plainly(monkeypatch):
    Experio(monkeypatch)
    assert "Aucune écriture comptabilisée" in reports.trial_balance(DOSSIER, "s1", account_prefix="7")
    assert "Aucun solde ouvert" in reports.partner_balances(DOSSIER, "s1", "4411")


@pytest.mark.functionality
def test_open_balances_are_experios_residual_so_a_part_payment_counts_only_what_is_left(monkeypatch):
    groups = [
        {"partner_id": [1, "PETIT"], "amount_residual": 10.0},
        {"partner_id": [2, "GROS"], "amount_residual": -900.0},
        {"partner_id": False, "amount_residual": 5.0},
    ]
    experio = Experio(monkeypatch, groups=groups)
    text = reports.partner_balances(DOSSIER, "s1", "4411")
    assert text.index("GROS") < text.index("PETIT") < text.index("sans tiers")
    assert "- GROS : créditeur 900.00" in text and "Total : créditeur 885.00." in text
    method, domain, more, kw = experio.calls[1]  # after the opening search
    assert method == "read_group" and more[0] == ["amount_residual:sum"]
    assert ("amount_residual", "!=", 0) in domain and not any(term[0] == "reconciled" for term in domain)


@pytest.mark.functionality
def test_open_balances_start_with_the_exercice_when_it_has_an_opening_else_at_the_origin(monkeypatch):
    experio = Experio(monkeypatch, groups=[{"partner_id": [1, "A"], "amount_residual": 1.0}], opening=True)
    with_opening = reports.partner_balances(DOSSIER, "s1", "4411")
    assert ("date", ">=", "2026-01-01") in experio.calls[1][1]
    assert "depuis le début de l'exercice, ouverture comprise" in with_opening
    experio.calls.clear()
    experio.opening = False
    without = reports.partner_balances(DOSSIER, "s1", "4411")
    assert not any(term[:2] == ("date", ">=") for term in experio.calls[1][1])
    assert "pris depuis l'origine" in without


@pytest.mark.communication
def test_open_balances_say_they_follow_todays_reconciliation(monkeypatch):
    Experio(monkeypatch, groups=[{"partner_id": [1, "A"], "amount_residual": 1.0}])
    assert "non lettrés à ce jour, écritures jusqu'au 31/12/2026" in reports.partner_balances(DOSSIER, "s1", "4411")


@pytest.mark.functionality
def test_monthly_totals_list_each_month_booked_in_french(monkeypatch):
    groups = [
        {"date:month": "janvier 2026", "debit": 0.0, "credit": 10.0},
        {"date:month": "février 2026", "debit": 4.0, "credit": 0.0},
    ]
    experio = Experio(monkeypatch, groups=groups)
    text = reports.monthly_totals(DOSSIER, "s1", "7")
    assert "- janvier 2026 : débit 0.00, crédit 10.00, solde créditeur 10.00" in text
    assert "- février 2026 : débit 4.00, crédit 0.00, solde débiteur 4.00" in text
    assert experio.calls[0][3]["orderby"] == "date:month" and experio.calls[0][3]["context"]["lang"] == "fr_FR"
    with pytest.raises(ToolError, match="account_prefix"):
        reports.monthly_totals(DOSSIER, "s1", "")


def a_line(n):
    return {
        "date": f"2026-02-0{n}",
        "journal_id": [1, "ACHAT"],
        "move_id": [n, f"FA{n}"],
        "name": "Achat",
        "partner_id": [7, "ATLAS"],
        "debit": 10.0,
        "credit": 0.0,
    }


@pytest.mark.functionality
def test_the_ledger_shows_its_lines_and_what_it_leaves_out(monkeypatch):
    experio = Experio(monkeypatch, lines=[a_line(n) for n in range(1, 4)])
    text = reports.account_ledger(DOSSIER, "s1", "6", limit=2)
    assert "3 ligne(s)" in text and "01/02/2026 | ACHAT | FA1 | Achat | ATLAS | débit 10.00 | crédit 0.00" in text
    assert "1 ligne(s) de plus non affichées" in text
    assert experio.calls[0][3]["limit"] == 2
    reports.account_ledger(DOSSIER, "s1", "6", limit=10_000)
    assert experio.calls[-1][3]["limit"] == reports.MAX_LINES or experio.calls[-2][3]["limit"] == reports.MAX_LINES
    reports.account_ledger(DOSSIER, "s1", "6", partner_id=7)
    assert any(("partner_id", "=", 7) in domain for _, domain, _, _ in experio.calls)
    for bad in ({"partner_id": "7"}, {"partner_id": True}, {"partner_id": -1}, {"limit": True}, {"limit": 0}):
        with pytest.raises(ToolError, match="entier"):
            reports.account_ledger(DOSSIER, "s1", "6", **bad)


@pytest.mark.communication
def test_the_ledger_and_the_open_balances_end_with_their_caveats(monkeypatch):
    Experio(monkeypatch, lines=[a_line(1)], opening=False)
    assert reports.OPEN_NOTE in reports.account_ledger(DOSSIER, "s1", "6")
    assert reports.NO_OPENING_NOTE in reports.account_ledger(DOSSIER, "s1", "4")


@pytest.mark.technical
def test_a_cut_list_gives_no_total_and_no_claim_of_a_full_ranking(monkeypatch):
    accounts = [row(f"6{n:07d}", "Compte", 1.0, 0.0) for n in range(reports.MAX_GROUPS + 1)]
    Experio(monkeypatch, groups=accounts)
    text = reports.trial_balance(DOSSIER, "s1", account_prefix="6")
    assert reports.CUT_NOTE in text and "Total :" not in text
    partners = [{"partner_id": [n, f"T{n}"], "amount_residual": 1.0} for n in range(reports.MAX_GROUPS + 1)]
    Experio(monkeypatch, groups=partners)
    text = reports.partner_balances(DOSSIER, "s1", "4411")
    assert reports.CUT_NOTE in text and "Total :" not in text and text.count("- T") == reports.MAX_GROUPS


@pytest.mark.technical
def test_a_report_too_long_for_the_conversation_is_refused(monkeypatch):
    long = [row(f"6{n:07d}", "x" * 900, 1.0, 0.0) for n in range(40)]
    Experio(monkeypatch, groups=long)
    with pytest.raises(ToolError, match="trop long"):
        reports.trial_balance(DOSSIER, "s1", account_prefix="6")


# --- What the agent hands to a report ---


@pytest.mark.security
@pytest.mark.parametrize(
    "closure, arguments",
    [
        (agent.trial_balance, {"dossier_id": 5}),
        (agent.monthly_totals, {"dossier_id": 5, "account_prefix": "7"}),
        (agent.partner_balances, {"dossier_id": 5, "account_prefix": "4411"}),
        (agent.account_ledger, {"dossier_id": 5, "account_prefix": "6"}),
    ],
)
def test_a_report_gets_the_conversation_of_the_run_never_the_models(monkeypatch, closure, arguments):
    seen = {}

    async def reading(runtime, tool_name, **given):
        seen.update(tool_name=tool_name, **given)
        return "ok"

    monkeypatch.setattr(agent, "reading", reading)
    runtime = SimpleNamespace(config={"configurable": {"thread_id": "t-1"}})
    assert asyncio.run(closure(runtime=runtime, **arguments)) == "ok"
    assert seen["session_id"] == "t-1" and seen["tool_name"] == closure.__name__


@pytest.mark.security
def test_the_agent_cannot_set_the_conversation_of_a_report():
    for name in ("trial_balance", "monthly_totals", "partner_balances", "account_ledger"):
        arguments = set(next(t for t in agent.TOOLS if t.name == f"Experio_{name}").args)
        assert "session_id" not in arguments and "dossier_id" in arguments, name
