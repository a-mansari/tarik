"""The run's summary, on the real Postgres (db_cabinet): every invoice's outcome, each under its own number."""

import pytest

from tarik.invoices.pipeline import record_step, run_summary


@pytest.mark.communication
def test_the_same_failure_on_two_invoices_names_each_one(db_cabinet):
    for invoice_id in (11, 12):  # what n8n records for a failed step: its error, with the invoice it was given
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
