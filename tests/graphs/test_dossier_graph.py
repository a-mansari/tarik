"""The Dossier graph's routing: which step runs after which, for each mode and each outcome."""

import pytest

from engine.dossier import graph

NOT_OK = ["stop", "question", "wait", "error"]
ONBOARDING = ["ask_banks", "finish_setup", "setup_status", "run_summary"]


@pytest.mark.functionality
def test_a_new_dossier_takes_its_models_configuration_then_its_files_then_is_onboarded(steps):
    steps.answer("create_dossier", {"status": "ok", "dossier_id": 7})
    steps.answer("files_to_process", {"status": "ok", "files": [1, 2]})
    out = steps.run(graph, name="Alpha", model_dossier_id=3, finish=True)
    assert steps.names == [
        "save_files",
        "check_creator",
        "create_dossier",
        "copy_configuration",
        "files_to_process",
        "import_entries",
        "import_entries",
        *ONBOARDING,
    ]
    assert steps.args("copy_configuration") == [{"dossier_id": 7, "model_dossier_id": 3}]
    assert [a["file_id"] for a in steps.args("import_entries")] == [1, 2]
    assert {a["dossier_id"] for a in steps.args("import_entries")} == {7}
    assert steps.args("finish_setup") == [{"dossier_id": 7, "seconds_left": 0, "finish": True}]
    assert steps.args("setup_status") == [{"dossier_id": 7}] and out["message"] == "resume"


@pytest.mark.functionality
@pytest.mark.parametrize("given", [{}, {"model_dossier_id": 3, "reused": True}])
def test_without_a_model_or_with_a_reused_dossier_no_configuration_is_copied(steps, given):
    steps.answer("create_dossier", {"status": "ok", "dossier_id": 7, "reused": given.get("reused", False)})
    steps.run(graph, model_dossier_id=given.get("model_dossier_id", 0))
    assert steps.names == ["save_files", "check_creator", "create_dossier", "files_to_process", *ONBOARDING]


@pytest.mark.functionality
def test_a_refused_configuration_copy_goes_no_further(steps):
    steps.answer("create_dossier", {"status": "ok", "dossier_id": 7})
    steps.answer("copy_configuration", {"status": "error", "message": "Le dossier a deja des ecritures."})
    steps.run(graph, model_dossier_id=3)
    assert steps.names == ["save_files", "check_creator", "create_dossier", "copy_configuration", "run_summary"]


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
def test_a_sender_who_may_not_create_dossiers_gets_only_the_summary(steps, status):
    steps.answer("check_creator", {"status": status})
    steps.run(graph, name="Alpha")
    assert steps.names == ["save_files", "check_creator", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
def test_a_dossier_not_created_goes_no_further(steps, status):
    steps.answer("create_dossier", {"status": status})
    steps.run(graph, name="Alpha", model_dossier_id=3)
    assert steps.names == ["save_files", "check_creator", "create_dossier", "run_summary"]


@pytest.mark.technical
@pytest.mark.parametrize("mode", ["", "update", "resume", "dossiers"])
def test_a_failed_save_of_the_attachments_goes_straight_to_the_summary(steps, mode):
    steps.answer("save_files", {"status": "error"})
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
def test_a_refused_import_stops_before_the_banks(steps, status):
    steps.answer("create_dossier", {"status": "ok", "dossier_id": 7})
    steps.answer("files_to_process", {"status": "ok", "files": [1, 2]})
    steps.answer("import_entries", {"status": "ok"}, {"status": status})
    steps.run(graph)
    assert steps.names == [
        "save_files",
        "check_creator",
        "create_dossier",
        "files_to_process",
        "import_entries",
        "import_entries",
        "run_summary",
    ]


@pytest.mark.functionality
def test_files_not_listed_go_no_further(steps):
    steps.answer("files_to_process", {"status": "error"})
    steps.run(graph, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "files_to_process", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
def test_a_dossier_not_finished_has_no_state_said(steps, status):
    steps.answer("finish_setup", {"status": status})
    steps.run(graph, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "files_to_process", "ask_banks", "finish_setup", "run_summary"]


@pytest.mark.functionality
def test_a_dossier_given_has_its_sender_checked_then_its_files_imported(steps):
    steps.answer("files_to_process", {"status": "ok", "files": [4]})
    steps.run(graph, dossier_id=5, kind="balance")
    assert steps.names == ["save_files", "check_sender", "files_to_process", "import_entries", *ONBOARDING]
    assert steps.args("check_sender") == [{"session_id": "s1", "dossier_id": 5}]
    assert steps.args("import_entries") == [{"dossier_id": 5, "file_id": 4, "kind": "balance"}]
    assert steps.args("finish_setup") == [{"dossier_id": 5, "seconds_left": 0, "finish": False}]


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("mode", ["", "update", "archive", "difference"])
def test_a_refused_sender_changes_nothing(steps, mode, status):
    steps.answer("check_sender", {"status": status})
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "mode, tool",
    [
        ("update", "update_dossier"),
        ("archive", "archive_dossier"),
        ("difference", "settle_difference"),
    ],
)
@pytest.mark.parametrize("status", ["ok", *NOT_OK])
def test_each_change_runs_its_one_step_after_the_sender_check(steps, mode, tool, status):
    steps.answer(tool, {"status": status})
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "check_sender", tool, "run_summary"]
    assert steps.args(tool)[0]["dossier_id"] == 5


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("mode", ["person", "access"])
def test_a_sender_who_may_not_manage_people_changes_no_access(steps, mode, status):
    steps.answer("check_manager", {"status": status})
    steps.run(graph, mode=mode, dossier_id=5)
    assert steps.names == ["save_files", "check_manager", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("mode, tool", [("person", "create_person"), ("access", "change_access")])
@pytest.mark.parametrize("status", ["ok", *NOT_OK])
def test_a_person_or_an_access_is_changed_after_the_manager_check(steps, mode, tool, status):
    steps.answer(tool, {"status": status})
    steps.run(graph, mode=mode, dossier_id=5, action="assign", email="samira@cabinet.ma")
    assert steps.names == ["save_files", "check_manager", tool, "run_summary"]
    assert steps.args(tool)[0]["dossier_id"] == 5 and steps.args("check_manager") == [{"session_id": "s1"}]
    if tool == "change_access":
        assert steps.args(tool)[0]["action"] == "assign" and steps.args(tool)[0]["email"] == "samira@cabinet.ma"


@pytest.mark.functionality
def test_a_routine_is_managed_after_the_manager_check(steps):
    steps.run(graph, mode="routine", action="create", routine_kind="tva", every="month", days=[5])
    assert steps.names == ["save_files", "check_manager", "manage_routine", "run_summary"]
    (given,) = steps.args("manage_routine")
    assert given["kind"] == "tva" and given["every"] == "month"
    assert given["days"] == [5] and given["hour"] == 8  # no hour given: 8 h


@pytest.mark.security
@pytest.mark.parametrize("status", NOT_OK)
def test_a_sender_who_may_not_manage_makes_no_routine(steps, status):
    steps.answer("check_manager", {"status": status})
    steps.run(graph, mode="routine", action="create")
    assert steps.names == ["save_files", "check_manager", "run_summary"]


@pytest.mark.functionality
def test_resume_finishes_the_waiting_dossiers_and_says_the_state_of_those_finished(steps):
    steps.answer("waiting_setups", {"status": "ok", "dossiers": [3, 4, 5]})
    steps.answer("finish_setup", lambda dossier_id: {"status": "wait" if dossier_id == 4 else "ok"})
    steps.run(graph, mode="resume", dossier_id=9)
    assert steps.names == [
        "save_files",
        "waiting_setups",
        "finish_setup",
        "finish_setup",
        "finish_setup",
        "setup_status",
        "setup_status",
        "run_summary",
    ]
    assert steps.args("finish_setup") == [{"dossier_id": 3}, {"dossier_id": 4}, {"dossier_id": 5}]
    assert [a["dossier_id"] for a in steps.args("setup_status")] == [3, 5]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "waiting, finished",
    [({"status": "ok", "dossiers": []}, "ok"), ({"status": "error"}, "ok"), ({"dossiers": [3]}, "wait")],
)
def test_resume_with_nothing_finished_goes_to_the_summary(steps, waiting, finished):
    steps.answer("waiting_setups", {"status": "ok", **waiting})
    steps.answer("finish_setup", {"status": finished})
    steps.run(graph, mode="resume")
    assert steps.names[:2] == ["save_files", "waiting_setups"] and steps.names[-1] == "run_summary"
    assert "setup_status" not in steps.names


@pytest.mark.functionality
def test_a_spreadsheet_of_dossiers_imports_each_file_after_the_creator_check(steps):
    steps.answer("files_to_process", {"status": "ok", "files": [11, 12]})
    steps.answer("import_dossiers", {"status": "question"}, {"status": "ok"})
    steps.run(graph, mode="dossiers", dossier_id=5, client_confirmed=True)
    assert steps.names == [
        "save_files",
        "check_creator",
        "files_to_process",
        "import_dossiers",
        "import_dossiers",
        "run_summary",
    ]
    assert [(a["file_id"], a["client_confirmed"]) for a in steps.args("import_dossiers")] == [(11, True), (12, True)]


@pytest.mark.functionality
@pytest.mark.parametrize("files", [{"status": "error"}, {"status": "ok", "files": []}])
def test_a_spreadsheet_not_found_imports_nothing(steps, files):
    steps.answer("files_to_process", files)
    steps.run(graph, mode="dossiers")
    assert steps.names == ["save_files", "check_creator", "files_to_process", "run_summary"]
