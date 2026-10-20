"""The run's summary, on the real Postgres (db_cabinet): every invoice's outcome, each under its own number."""

import pytest

from tarik.invoices.pipeline import record_step, run_summary
from tests import runs


@pytest.mark.communication
def test_the_same_failure_on_two_invoices_names_each_one(db_cabinet):
    for invoice_id in (
        11,
        12,
    ):  # what engine.tools.step records for a failed step: its error, with the invoice it was given
        record_step(
            "r",
            "s",
            "Lecture Experio (wait_until_read)",
            {
                "status": "error",
                "message": "Experio est injoignable : la facture n'a pas pu etre lue.",
                "invoice_id": invoice_id,
            },
        )
    record_step(
        "r",
        "s",
        "Verification (check_invoice)",
        {"status": "ok", "message": "Facture 13 : conforme.", "invoice_id": 13},
    )
    summary = run_summary("r")["message"]
    assert "facture 11 : Experio est injoignable" in summary and "facture 12 : Experio est injoignable" in summary
    assert "facture 13" not in summary.lower().replace(
        "facture 13 : conforme", ""
    )  # a message naming it is left as it is


@pytest.mark.communication
def test_a_long_run_s_summary_keeps_what_the_client_needs_and_drops_the_rest(db_cabinet):
    runs.record("new")
    runs.record("old", routine=False)  # as before outcomes were marked: every one shown
    new, old = run_summary("new")["message"], run_summary("old")["message"]
    for message in runs.kept():  # every question, stop and problem, word for word
        assert message in new, message
    for invoice_id in range(21, 31):  # every invoice, with where it stands
        assert f"facture {invoice_id} :" in new.lower()
    for invoice_id in runs.POSTED:
        block = next(block for block in new.split("\n\n") if f"Facture {invoice_id} :" in block)
        assert block.splitlines() == [  # who, which account, posted: the rest was routine
            f"- tiers : Facture {invoice_id} : fournisseur {block.split('fournisseur ')[1].split(',')[0]}, "
            f"Compte Tiers fournisseur 4411{invoice_id}.",
            f"- compte : Facture {invoice_id} : compte 6125 Achats de fournitures de bureau (compte memorise pour ce "
            "tiers).",
            f"- comptabilisation : Facture {invoice_id} : comptabilisee.",
        ]
    assert "TVA non recuperable" in new and "exercice 2025 cree" in new  # what changed is told
    assert "conforme" not in new and "pas de doublon" not in new and "journal Achats" not in new
    lines, old_lines = len(new.splitlines()), len(old.splitlines())
    print(f"\nsummary: {old_lines} lines, {len(old)} characters before; {lines} lines, {len(new)} characters now")
    assert lines <= old_lines / 3 and len(new) <= len(old) / 3
