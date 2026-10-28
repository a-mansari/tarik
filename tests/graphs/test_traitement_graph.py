"""The Traitement graph's routing: where a message goes, each file's run, each invoice's steps, the drafts taken up
again and where an answer resumes the sequence."""

import pytest

from engine.pointage import STEPS
from engine.tools.invoices.pipeline import INVOICE_CHAIN
from engine.traitement import graph

NOT_OK = ["stop", "question", "wait", "error"]
FRONT = ["save_files", "route_message", "resolve_dossier", "check_sender", "files_to_process"]
STOP = {"status": "stop"}


def routed(steps, route="files", files=(), **answers):
    steps.answer("route_message", {"route": route})
    steps.answer("resolve_dossier", {"status": "ok", "dossier_id": 5})
    steps.answer("files_to_process", {"status": "ok", "files": list(files)})
    for tool, result in answers.items():
        steps.answer(tool, result)


def invoice_ids(steps, tool):
    return [a["invoice_id"] for a in steps.args(tool)]


@pytest.mark.functionality
def test_a_message_without_files_takes_up_the_waiting_drafts(steps):
    routed(steps)
    out = steps.run(graph, mode="files", dossier="Alpha", comptabiliser="oui")
    assert steps.names == [*FRONT, "waiting_drafts", "run_summary"] and out["message"] == "resume"
    given = steps.args("waiting_drafts")[0]
    assert (given["dossier_id"], given["held"], given["stopped"]) == (5, True, True)


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("tool", ["resolve_dossier", "check_sender", "files_to_process"])
def test_a_dossier_not_found_a_refused_sender_or_files_not_listed_go_no_further(steps, tool, status):
    routed(steps, files=[11], **{tool: {"status": status}})
    steps.run(graph, mode="files")
    assert steps.names == [*FRONT[: FRONT.index(tool) + 1], "run_summary"]


@pytest.mark.technical
def test_a_failed_save_of_the_attachments_goes_straight_to_the_summary(steps):
    routed(steps, save_files={"status": "error"})
    steps.run(graph, mode="files")
    assert steps.names == ["save_files", "run_summary"]


@pytest.mark.functionality
def test_the_words_dossier_dimport_make_the_files_one_import_folder_first(steps):
    routed(steps, files=[11, 12], create_import_folder={"status": "ok", "invoices": [31, 32]}, wait_until_read=STOP)
    steps.run(graph, mode="files", client_message="Voici le dossier d'import.")
    assert steps.names == [*FRONT, "create_import_folder", "wait_until_read", "wait_until_read", "run_summary"]
    assert invoice_ids(steps, "wait_until_read") == [31, 32]
    assert "identify_document" not in steps.names


@pytest.mark.functionality
@pytest.mark.parametrize(
    "made, following",
    [({"status": "ok", "invoices": []}, ["waiting_drafts"]), ({"status": "error"}, [])],
)
def test_an_import_folder_with_no_drafts_or_refused_goes_to_the_waiting_drafts_or_the_summary(steps, made, following):
    routed(steps, files=[11], create_import_folder=made)
    steps.run(graph, mode="files", client_message="dossier d'import")
    assert steps.names == [*FRONT, "create_import_folder", *following, "run_summary"]


@pytest.mark.functionality
def test_the_import_folder_words_without_files_take_up_the_waiting_drafts(steps):
    routed(steps)
    steps.run(graph, mode="files", client_message="dossier d'import")
    assert steps.names == [*FRONT, "waiting_drafts", "run_summary"]


@pytest.mark.functionality
def test_each_new_file_has_a_run_of_its_own_in_order(steps):
    routed(steps, files=[11, 12], identify_document=STOP)
    steps.run(graph, mode="files")
    assert steps.names == [*FRONT, "identify_document", "identify_document", "run_summary"]
    assert [a["file_id"] for a in steps.args("identify_document")] == [11, 12]


@pytest.mark.functionality
def test_an_invoice_goes_through_every_step_of_its_chain_in_order(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [21]},
        create_draft_invoice={"status": "ok", "invoice_id": 40},
    )
    steps.run(graph, mode="files")
    assert steps.names == [*FRONT, "identify_document", "split_invoices", *INVOICE_CHAIN, "run_summary"]
    assert steps.args("create_draft_invoice")[0] | {"invoice_type": ""} == {
        "file_id": 21,
        "dossier_id": 5,
        "invoice_type": "",
    }
    assert set(invoice_ids(steps, "post_invoice") + invoice_ids(steps, "check_duplicate")) == {40}


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("tool", ["create_draft_invoice", "correct_invoice", "check_duplicate", "hold_posting"])
def test_an_invoice_stops_at_the_first_step_not_ok(steps, tool, status):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [21]},
        **{tool: {"status": status, "invoice_id": 40}},
    )
    steps.run(graph, mode="files")
    chain = INVOICE_CHAIN[: INVOICE_CHAIN.index(tool) + 1]
    assert steps.names == [*FRONT, "identify_document", "split_invoices", *chain, "run_summary"]


@pytest.mark.functionality
def test_each_invoice_cut_out_of_a_file_has_its_own_run(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [21, 22]},
        create_draft_invoice=STOP,
    )
    steps.run(graph, mode="files")
    assert [a["file_id"] for a in steps.args("create_draft_invoice")] == [21, 22]


def split_off(steps, *others):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [21]},
        create_draft_invoice={"status": "ok", "invoice_id": 40},
        wait_until_read=lambda invoice_id, run_id: {
            "status": "ok",
            "split_off": list(others) if invoice_id == 40 else [],
        },
        check_invoice=STOP,
    )
    steps.run(graph, mode="files")


@pytest.mark.functionality
def test_a_draft_experio_split_off_a_bill_runs_once_after_it(steps):
    split_off(steps, 41)
    assert invoice_ids(steps, "wait_until_read") == [40, 41]
    assert invoice_ids(steps, "check_invoice") == [40, 41] and steps.names[-1] == "run_summary"


@pytest.mark.functionality
def test_the_drafts_experio_split_off_a_bill_each_run_once_after_it(steps):
    split_off(steps, 41, 42)
    assert invoice_ids(steps, "wait_until_read") == [40, 41, 42]


@pytest.mark.functionality
@pytest.mark.parametrize("status", NOT_OK)
@pytest.mark.parametrize("tool", ["identify_document", "split_invoices"])
def test_a_file_not_identified_or_not_split_goes_no_further(steps, tool, status):
    answers = {"identify_document": {"status": "ok", "kind": "invoice"}, tool: {"status": status}}
    routed(steps, files=[11], **answers)
    steps.run(graph, mode="files")
    file_steps = ["identify_document", "split_invoices"]
    assert steps.names == [*FRONT, *file_steps[: file_steps.index(tool) + 1], "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "imported, comptabiliser, following",
    [
        ({"status": "ok", "statement_id": 8}, "oui", ["settle_statement", "post_statement"]),
        ({"status": "ok", "statement_id": 8}, "", ["settle_statement"]),
        ({"status": "ok"}, "oui", []),
        ({"status": "question", "statement_id": 8}, "oui", []),
    ],
)
def test_a_statement_is_imported_pointed_then_posted_only_when_asked(steps, imported, comptabiliser, following):
    routed(steps, files=[11], identify_document={"status": "ok", "kind": "statement"}, import_bank_statement=imported)
    steps.run(graph, mode="files", comptabiliser=comptabiliser)
    assert steps.names == [*FRONT, "identify_document", "import_bank_statement", *following, "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("comptabiliser, posted", [("oui", ["post_statement"]), ("", [])])
def test_a_bank_statements_lines_are_pointed_step_by_step_then_posted_when_asked(steps, comptabiliser, posted):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "statement"},
        import_bank_statement={"status": "ok", "statement_id": 8},
        settle_statement={"status": "ok", "routine": True, "statement_id": 8, "bank": True},
    )
    steps.run(graph, mode="files", comptabiliser=comptabiliser, client_message="la FA-12")
    pointing = [*STEPS.values(), "report_statement"]
    assert steps.names[-len(pointing) - len(posted) - 2 :] == ["settle_statement", *pointing, *posted, "run_summary"]
    assert steps.args("tie_cited_invoices")[0] == {"statement_id": 8, "client_message": "la FA-12"}


@pytest.mark.functionality
def test_a_bank_statement_whose_pointing_stops_is_not_posted(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "statement"},
        import_bank_statement={"status": "ok", "statement_id": 8},
        settle_statement={"status": "ok", "routine": True, "statement_id": 8, "bank": True},
        choose_line_operations={"status": "error"},
    )
    steps.run(graph, mode="files", comptabiliser="oui")
    assert steps.names[-3:] == ["detect_statement_lines", "choose_line_operations", "run_summary"]


@pytest.mark.functionality
def test_a_statement_not_pointed_is_not_posted(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "statement"},
        import_bank_statement={"status": "ok", "statement_id": 8},
        settle_statement={"status": "wait"},
    )
    steps.run(graph, mode="files", comptabiliser="oui")
    assert steps.names[-2:] == ["settle_statement", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "answered, following",
    [
        ({"status": "question"}, []),
        ({"status": "ok"}, []),
        ({"status": "ok", "next_step": "waiting_drafts"}, ["waiting_drafts"]),
        ({"status": "ok", "next_step": "settle_tva", "invoice_id": 5}, ["settle_tva", "check_journal"]),
        ({"status": "ok", "next_step": "settle_statement", "file_id": 8}, ["settle_statement"]),
        (
            {"status": "ok", "next_step": "identify_document", "file_id": 8, "dossier_id": 5},
            ["identify_document"],
        ),
    ],
)
def test_an_answer_resumes_the_sequence_at_its_next_step(steps, answered, following):
    routed(steps, "answer", answer_pending=answered, check_journal=STOP, identify_document=STOP)
    steps.run(graph, mode="answer", answer="oui")
    assert steps.names == ["save_files", "route_message", "answer_pending", *following, "run_summary"]


@pytest.mark.functionality
def test_an_account_given_for_another_invoice_with_the_answer_is_asked_to_be_confirmed(steps):
    answered = {"status": "ok", "next_step": "settle_tva", "invoice_id": 5, "corrections": [[6, "6111"]]}
    routed(steps, "answer", answer_pending=answered, settle_tva=STOP, assign_account=STOP)
    steps.run(graph, mode="answer")
    assert steps.names == [
        "save_files",
        "route_message",
        "answer_pending",
        "settle_tva",
        "assign_account",
        "run_summary",
    ]
    given = steps.args("assign_account")[0]
    assert (given["invoice_id"], given["answer"], given["confirm"]) == (6, "6111", True)


@pytest.mark.functionality
@pytest.mark.parametrize("answered", [{"status": "ok", "next_step": "settle_tva"}, {"status": "question"}])
def test_the_invoice_answered_about_and_given_an_account_goes_back_to_its_account_step(steps, answered):
    routed(steps, "answer", answer_pending={**answered, "invoice_id": 5, "corrections": [[5, "6111"]]})
    steps.answer("assign_account", STOP)
    steps.run(graph, mode="answer")
    assert steps.names == ["save_files", "route_message", "answer_pending", "assign_account", "run_summary"]
    given = steps.args("assign_account")[0]
    assert (given["invoice_id"], given["answer"], given["confirm"]) == (5, "6111", False)


@pytest.mark.functionality
@pytest.mark.parametrize("corrected", [6, 5])
def test_the_drafts_taken_up_after_an_answer_leave_out_the_corrected_ones(steps, corrected):
    answered = {"status": "ok", "next_step": "waiting_drafts", "invoice_id": 5, "corrections": [[corrected, "6111"]]}
    routed(steps, "answer", answer_pending=answered, assign_account=STOP, wait_until_read=STOP)
    steps.answer("waiting_drafts", {"status": "ok", "invoices": [5, 6, 7]})
    steps.run(graph, mode="answer")
    assert invoice_ids(steps, "assign_account") == [corrected]
    assert invoice_ids(steps, "wait_until_read") == [i for i in (5, 6, 7) if i != corrected]
    assert steps.names.count("waiting_drafts") == 1 and steps.names[-1] == "run_summary"


@pytest.mark.functionality
def test_a_follow_up_takes_up_each_kind_of_waiting_draft_at_its_own_step(steps):
    found = {"status": "ok", "invoices": [5, 9], "corrections": [[9, "6111"]], "facturation": [8], "statements": [12]}
    routed(steps, "resume", waiting_drafts=found)
    for tool in ("wait_until_read", "assign_account", "configure_cabinet", "settle_statement"):
        steps.answer(tool, STOP)
    steps.run(graph, mode="resume", compte="4411")
    assert steps.names == [
        "save_files",
        "route_message",
        "waiting_drafts",
        "wait_until_read",
        "assign_account",
        "configure_cabinet",
        "settle_statement",
        "run_summary",
    ]
    assert steps.args("waiting_drafts")[0]["stopped"] is False
    assert (steps.args("assign_account")[0]["answer"], steps.args("assign_account")[0]["confirm"]) == ("6111", True)
    assert [invoice_ids(steps, "wait_until_read"), steps.args("settle_statement")[0]["file_id"]] == [[5], 12]


@pytest.mark.functionality
def test_a_follow_up_with_nothing_waiting_goes_to_the_summary(steps):
    routed(steps, "resume")
    steps.run(graph, mode="resume")
    assert steps.names == ["save_files", "route_message", "waiting_drafts", "run_summary"]


@pytest.mark.functionality
@pytest.mark.parametrize("route, comptabiliser", [("both", ""), ("answer", "oui")])
def test_an_answer_with_files_or_asked_to_post_does_both_in_one_run(steps, route, comptabiliser):
    routed(steps, route, answer_pending={"status": "question"})
    steps.run(graph, mode="files", comptabiliser=comptabiliser)
    # each branch ends at the summary: the last one is said after both
    assert set(steps.names) == {*FRONT, "answer_pending", "waiting_drafts", "run_summary"}
    assert steps.names.count("answer_pending") == steps.names.count("waiting_drafts") == 1
    assert steps.names[-1] == "run_summary"


@pytest.mark.technical
@pytest.mark.parametrize(
    "mode, routing, first",
    [
        ("answer", RuntimeError("down"), "answer_pending"),
        ("resume", RuntimeError("down"), "waiting_drafts"),
        (None, RuntimeError("down"), "resolve_dossier"),
        ("answer", {"route": "anything"}, "resolve_dossier"),
    ],
)
def test_without_the_databases_route_the_mode_decides_and_an_unknown_one_means_files(steps, mode, routing, first):
    routed(steps, route_message=routing, answer_pending={"status": "question"})
    steps.run(graph, mode=mode)
    assert steps.names[2] == first


@pytest.mark.functionality
def test_a_file_sent_again_takes_its_draft_up_from_experios_reading(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [], "resume_invoice": 272493},
        wait_until_read=STOP,
    )
    steps.run(graph, mode="files")
    assert "create_draft_invoice" not in steps.names and invoice_ids(steps, "wait_until_read") == [272493]


@pytest.mark.functionality
def test_a_file_sent_again_already_posted_makes_nothing(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": []},
    )
    steps.run(graph, mode="files")
    assert "create_draft_invoice" not in steps.names and "wait_until_read" not in steps.names


@pytest.mark.functionality
def test_the_side_read_on_the_scan_makes_the_draft_as_that_side(steps):
    routed(
        steps,
        files=[11],
        identify_document={"status": "ok", "kind": "invoice"},
        split_invoices={"status": "ok", "files": [21], "invoice_type": "vente"},
        create_draft_invoice=STOP,
    )
    steps.run(graph, mode="files")
    assert steps.args("create_draft_invoice")[0]["invoice_type"] == "vente"
    assert steps.args("split_invoices")[0]["dossier_id"] == 5
