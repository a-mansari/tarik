"""The run's summary, on the real Postgres (db_cabinet): every invoice's outcome, each under its own number, in
sections the client reads in order (Fait, En attente, Doublons, À corriger, Questions)."""

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
    problems = summary.split("**À corriger**\n")[1].splitlines()
    for invoice_id in (11, 12):
        assert any(
            line.startswith(f"- Facture {invoice_id} : ") and "Experio est injoignable" in line for line in problems
        )
    assert "- Facture 13 : conforme." in summary.split("**À corriger**")[0]  # a message naming it is left as it is


@pytest.mark.communication
def test_a_long_run_s_summary_keeps_what_the_client_needs_and_drops_the_rest(db_cabinet):
    runs.record("new")
    runs.record("old", routine=False)  # as before outcomes were marked: every one shown
    new, old = run_summary("new")["message"], run_summary("old")["message"]
    for message in runs.kept():  # every question, stop and problem, word for word
        assert message in new, message
    for invoice_id in range(21, 31):  # every invoice, with where it stands
        assert f"facture {invoice_id} :" in new.lower()
    sections = [line for line in new.splitlines() if line.startswith("**")]
    assert sections == ["**Fait**", "**Doublons, non traités**", "**À corriger**", "**Questions**"]
    done, rest = new.split("**Doublons, non traités**")
    for invoice_id in runs.POSTED:  # one line: who, which account, posted; the rest was routine
        (line,) = [line for line in done.splitlines() if line.startswith(f"- Facture {invoice_id} : ")]
        assert line.endswith(
            "compte 6125 Achats de fournitures de bureau (compte memorise pour ce tiers); comptabilisee."
        )
        assert f"Compte Tiers fournisseur 4411{invoice_id}" in line
    assert "Facture 27" in rest.split("**À corriger**")[0] and "Facture 27" not in done  # a duplicate, only there
    assert "Facture 30 : problème à l'étape vérification" in rest.split("**Questions**")[0]
    questions = rest.split("**Questions**\n")[1].splitlines()
    assert [line[:3] for line in questions] == ["1. ", "2. "] and all("QUESTION : Facture" in q for q in questions)
    assert "Facture 29" not in done  # where it stands: its question, not what was done before it
    assert "TVA non recuperable" in new and "exercice 2025 cree" in new  # what changed is told
    assert "conforme" not in new and "pas de doublon" not in new and "journal Achats" not in new
    lines, old_lines = len(new.splitlines()), len(old.splitlines())
    print(f"\nsummary: {old_lines} lines, {len(old)} characters before; {lines} lines, {len(new)} characters now")
    assert lines <= 20 and len(new) <= len(old) / 2  # one line per invoice, routine outcomes left out


@pytest.mark.communication
def test_a_draft_kept_until_the_client_says_to_post_it_waits_it_is_nothing_to_correct(db_cabinet):
    held = "Facture 14 : importée et vérifiée, laissée en brouillon. Dites-moi quand la comptabiliser."
    record_step("r", "s", "hold_posting", {"status": "stop", "message": held, "invoice_id": 14})
    assert run_summary("r")["message"] == f"**En attente**\n- {held}"


@pytest.mark.communication
def test_an_invoice_waits_on_one_question_its_latest(db_cabinet):
    for asked in (
        "Facture 9 : « Objet : X » ne désigne pas un seul compte.",
        "Facture 9 : « 6142 » ne désigne pas un seul compte.",
    ):
        record_step("r", "s", "assign_account", {"status": "question", "message": asked, "invoice_id": 9})
    assert (
        run_summary("r")["message"]
        == "**Questions**\n1. QUESTION : Facture 9 : « 6142 » ne désigne pas un seul compte."
    )
