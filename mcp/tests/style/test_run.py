"""tests/run.py picks the tests that cover a change, and only those."""

import pytest

from tests import run


def chosen(*changed):
    return {str(test.relative_to(run.TESTS)) for test in run.selected({run.MCP / path for path in changed})}


@pytest.mark.technical
def test_a_change_runs_the_tests_that_import_it():
    tests = chosen("tarik/invoices/tva.py")
    assert {"invoices/test_tva.py", "isolation/test_no_setting_shown.py", "style/test_consistency.py"} <= tests
    assert not {"core/test_vault.py", "dossiers/test_search.py", "isolation/test_cabinet_scope.py"} & tests


@pytest.mark.technical
def test_a_file_no_import_reaches_is_mapped():
    assert "isolation/test_cabinet_postgres.py" in chosen("tarik/core/cabinet.sql")
    assert chosen("pytest.ini") == {str(test.relative_to(run.TESTS)) for test in run.TESTS.rglob("test_*.py")}


@pytest.mark.technical
def test_a_workflow_change_runs_every_live_n8n_test():
    tests = {str(test.relative_to(run.TESTS)) for test in run.selected({run.REPO / "workflows/steps.py"})}
    assert {"n8n/test_conversation.py", "n8n/test_at_the_end.py", "style/test_consistency.py"} <= tests
