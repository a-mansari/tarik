"""The Relevés graph's routing: the sender checked, the dossier's open statements listed, each pointed step by step,
posted only when asked."""

import pytest

from engine.pointage import STEPS
from engine.releves import graph

NOT_OK = ["stop", "question", "wait", "error"]
POINTING = [*STEPS.values(), "report_statement"]


def listed(steps, *ids):
    steps.answer("list_open_statements", {"status": "ok", "routine": True, "statement_ids": list(ids)})


@pytest.mark.functionality
def test_each_open_statement_is_pointed_step_by_step_and_not_posted_unasked(steps):
    listed(steps, 40, 41)
    out = steps.run(graph, dossier_id=5, client_message="la facture FA-12")
    front = ["save_files", "check_sender", "list_open_statements"]
    assert steps.names == [*front, *POINTING, *POINTING, "run_summary"] and out["message"] == "resume"
    assert [a["statement_id"] for a in steps.args("report_statement")] == [40, 41]
    assert steps.args("tie_cited_invoices")[0] == {"statement_id": 40, "client_message": "la facture FA-12"}


@pytest.mark.functionality
def test_the_report_gathers_what_each_step_did(steps):
    listed(steps, 40)
    steps.answer("detect_statement_lines", {"status": "ok", "said": "2 opérations ont été pointées.", "note": ""})
    steps.answer(
        "tie_cited_invoices",
        {"status": "ok", "said": "", "note": "Je n'ai pas rattaché la facture X que vous avez citée."},
    )
    steps.run(graph, dossier_id=5)
    report = steps.args("report_statement")[0]
    assert report["said"] == ["2 opérations ont été pointées."] and report["notes"] == [
        "Je n'ai pas rattaché la facture X que vous avez citée."
    ]


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
def test_a_step_not_ok_ends_that_statements_pointing_only(steps, status):
    listed(steps, 40, 41)
    steps.answer("name_line_tiers", {"status": status}, {"status": "ok"})
    steps.run(graph, dossier_id=5)
    assert steps.names.count("report_statement") == 1 and steps.names.count("detect_statement_lines") == 2


@pytest.mark.functionality
def test_the_statements_are_posted_when_the_client_asks(steps):
    listed(steps, 40)
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert steps.names[-2:] == ["post_dossier_statements", "run_summary"]
    assert steps.args("post_dossier_statements")[0] == {"dossier_id": 5}


@pytest.mark.functionality
@pytest.mark.parametrize("result", [{"status": "error"}, {"status": "ok", "routine": True, "statement_ids": []}])
def test_nothing_is_pointed_nor_posted_after_a_failed_listing_or_without_statements(steps, result):
    steps.answer("list_open_statements", result)
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert steps.names == ["save_files", "check_sender", "list_open_statements", "run_summary"]


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
def test_a_refused_sender_points_nothing(steps, status):
    steps.answer("check_sender", {"status": status})
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert steps.names == ["save_files", "check_sender", "run_summary"]
