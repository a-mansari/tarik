"""The tests set their settings in the environment: Vault is left out, but by the tests that check
the running services (tests/n8n), which read their token from it. db_cabinet: a throwaway cabinet on
the real Postgres, shared by the whole run and emptied after each test."""

import psycopg
import pytest

from tarik.core import cabinets, vault


@pytest.fixture(autouse=True)
def settings_from_the_environment(request, monkeypatch):
    if "tests/n8n/" not in str(request.path):
        monkeypatch.setattr(vault, "ENABLED", False)


DB_CABINET = "tarik_test_db"


@pytest.fixture(scope="session")
def _db_schema():
    """One throwaway cabinet schema for the whole run, made once (its tables from cabinet.sql)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cabinets, "CABINETS", frozenset({DB_CABINET}))
        cabinets.create_schemas()
    yield
    with psycopg.connect(**cabinets.POSTGRES) as admin:
        admin.execute(f"drop schema if exists {DB_CABINET} cascade")


@pytest.fixture
def db_cabinet(_db_schema, monkeypatch):
    """The test runs in that cabinet, on the real Postgres; every table is emptied after it, so tests never
    see each other's rows."""
    monkeypatch.setattr(cabinets, "CABINETS", frozenset({DB_CABINET}))
    try:
        with cabinets.using(DB_CABINET):
            yield DB_CABINET
    finally:
        with psycopg.connect(**cabinets.POSTGRES) as admin:
            tables = [name for (name,) in admin.execute("select tablename from pg_tables where schemaname = %s", (DB_CABINET,))]
            admin.execute(f"truncate {', '.join(f'{DB_CABINET}.{table}' for table in tables)} restart identity cascade")
