"""The Relevés graph's routing: the sender checked, the dossier's statements pointed, posted only when asked."""

import pytest

from engine.releves import graph

NOT_OK = ["stop", "question", "wait", "error"]


@pytest.mark.functionality
def test_the_statements_are_pointed_after_the_sender_check_and_not_posted_unasked(steps):
    out = steps.run(graph, dossier_id=5, client_message="pointez les relevés, facture FA-12")
    assert steps.names == ["save_files", "check_sender", "settle_dossier_statements", "run_summary"]
    assert steps.args("settle_dossier_statements")[0] == {
        "dossier_id": 5,
        "client_message": "pointez les relevés, facture FA-12",
    }
    assert out["message"] == "resume"


@pytest.mark.functionality
def test_the_statements_are_posted_when_the_client_asks(steps):
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert steps.names[-2:] == ["post_dossier_statements", "run_summary"]
    assert steps.args("post_dossier_statements")[0] == {"dossier_id": 5}


@pytest.mark.functionality
@pytest.mark.parametrize("result", [{"status": "stop"}, {"status": "ok", "routine": True}])
def test_nothing_is_posted_after_a_failed_pointing_or_without_statements(steps, result):
    steps.answer("settle_dossier_statements", result)
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert "post_dossier_statements" not in steps.names


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
def test_a_refused_sender_points_nothing(steps, status):
    steps.answer("check_sender", {"status": status})
    steps.run(graph, dossier_id=5, comptabiliser="oui")
    assert steps.names == ["save_files", "check_sender", "run_summary"]
