"""The tests set their settings in the environment: Vault is left out, but by the tests that check
the running services (tests/n8n), which read their token from it. Their data lives in the test database
(testdb.py), never the cabinets' own; db_cabinet is a throwaway cabinet there, shared by the whole run and
emptied before and after each test."""

import pytest

from tarik.core import cabinets, vault
from tests import testdb

DB_CABINET = testdb.schema("db")


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """The test database ready and this run's lock held, for the whole run."""
    held = testdb.start()
    yield
    held.close()


@pytest.fixture(autouse=True)
def settings_from_the_environment(request, monkeypatch):
    """Settings from the environment and data in the test database; tests/n8n alone check the running
    services, with Vault and the cabinets' own database."""
    if "tests/n8n/" not in str(request.path):
        monkeypatch.setattr(vault, "ENABLED", False)
        monkeypatch.setitem(cabinets.POSTGRES, "dbname", testdb.DATABASE)


@pytest.fixture(scope="session")
def _db_schema(_test_database):
    """One throwaway cabinet schema for the whole run, made once (its tables from cabinet.sql)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cabinets, "CABINETS", frozenset({DB_CABINET}))
        patch.setitem(cabinets.POSTGRES, "dbname", testdb.DATABASE)
        cabinets.create_schemas()
    yield
    testdb.drop(DB_CABINET)


@pytest.fixture
def db_cabinet(_db_schema, monkeypatch):
    """The test runs in that cabinet, emptied before and after it: tests never see each other's rows, even
    after a run cut short."""
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({DB_CABINET}))
    testdb.empty(DB_CABINET)
    try:
        with cabinets.using(DB_CABINET):
            yield DB_CABINET
    finally:
        testdb.empty(DB_CABINET)
