"""The Export graph's routing: the sender checked, then Experio's export."""

import pytest

from engine.exports import graph

NOT_OK = ["stop", "question", "wait", "error"]


@pytest.mark.functionality
@pytest.mark.parametrize("status", ["ok", *NOT_OK])
def test_the_export_runs_after_the_sender_check_then_the_summary(steps, status):
    steps.answer("export_entries", {"status": status})
    out = steps.run(graph, dossier_id=5, quoi="fec", periode="2025", integration=True)
    assert steps.names == ["save_files", "check_sender", "export_entries", "run_summary"] and out["message"] == "resume"
    given = steps.args("export_entries")[0]
    assert (given["dossier_id"], given["quoi"], given["periode"]) == (5, "fec", "2025")
    assert (given["integration"], given["lettrage"], given["client_confirmed"]) == (True, False, False)


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
def test_a_refused_sender_exports_nothing(steps, status):
    steps.answer("check_sender", {"status": status})
    steps.run(graph, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "run_summary"]


@pytest.mark.technical
def test_a_failed_save_of_the_attachments_goes_straight_to_the_summary(steps):
    steps.answer("save_files", {"status": "error"})
    steps.run(graph, dossier_id=5)
    assert steps.names == ["save_files", "run_summary"]
