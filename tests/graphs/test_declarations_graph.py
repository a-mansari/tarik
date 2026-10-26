"""The Déclarations graph's routing: the sender checked, then the one declaration the mode names."""

import pytest

from engine.declarations import graph

NOT_OK = ["stop", "question", "wait", "error"]
TOOLS = {
    "tva": "prepare_tva_declaration",
    "ras": "prepare_ras_declaration",
    "delais": "prepare_payment_delay_declaration",
}


@pytest.mark.functionality
@pytest.mark.parametrize("mode", list(TOOLS))
@pytest.mark.parametrize("status", ["ok", *NOT_OK])
def test_each_mode_runs_its_one_declaration_after_the_sender_check(steps, mode, status):
    steps.answer(TOOLS[mode], {"status": status})
    out = steps.run(graph, mode=mode, dossier_id=5, periode="T3", declarer=True)
    assert steps.names == ["save_files", "check_sender", TOOLS[mode], "run_summary"] and out["message"] == "resume"
    given = steps.args(TOOLS[mode])[0]
    assert (given["dossier_id"], given["periode"], given["declarer"], given["client_confirmed"]) == (
        5,
        "T3",
        True,
        False,
    )


@pytest.mark.functionality
def test_each_declaration_takes_its_own_words(steps):
    steps.run(graph, mode="tva", dossier_id=5, credit_reporte=1200)
    steps.run(graph, mode="ras", dossier_id=5, declarations="honoraires")
    steps.run(graph, mode="delais", dossier_id=5, chiffre_affaires=5000000)
    assert steps.args("prepare_tva_declaration")[0]["credit_reporte"] == "1200"
    assert steps.args("prepare_ras_declaration")[0]["declarations"] == "honoraires"
    assert steps.args("prepare_payment_delay_declaration")[0]["chiffre_affaires"] == "5000000"


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("mode", list(TOOLS))
def test_a_refused_sender_goes_to_the_summary(steps, mode, status):
    steps.answer("check_sender", {"status": status})
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("mode", [None, "", "export"])
def test_an_unknown_mode_declares_nothing(steps, mode):
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "run_summary"]


@pytest.mark.technical
def test_a_failed_save_of_the_attachments_goes_straight_to_the_summary(steps):
    steps.answer("save_files", {"status": "error"})
    steps.run(graph, mode="tva", dossier_id=5)
    assert steps.names == ["save_files", "run_summary"]
