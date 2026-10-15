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
