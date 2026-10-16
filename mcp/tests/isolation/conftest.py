"""Two throwaway cabinets (shared.A, shared.B) in the test database (tests/testdb.py), and tarik-mcp served on
a local port (shared.py): the fixtures the cabinet isolation tests share. server.py is imported only by
the fixtures that serve it, so a test that doesn't is not run for every tool's change (tests/run.py)."""

import pytest

from tarik.core import cabinets

from shared import A, B, serving
from tests import testdb


@pytest.fixture
def two_cabinets(monkeypatch):
    import server as mcp_server
    names = frozenset({A, B})
    monkeypatch.setattr(cabinets, "CABINETS", names)
    monkeypatch.setattr(mcp_server, "CABINETS", names)
    for name, uid in ((A, "11"), (B, "22")):
        monkeypatch.setenv(f"{name.upper()}__ODOO_UID", uid)
        monkeypatch.setenv(f"{name.upper()}__ODOO_USER", f"tarik@{name}")
        monkeypatch.setenv(f"{name.upper()}__ODOO_PASSWORD", f"secret-{name}")
        monkeypatch.setenv(f"{name.upper()}__MAIL_ALLOWED_RECIPIENTS", f"client@{name}.ma")
    return names


@pytest.fixture
def schemas(two_cabinets):
    cabinets.create_schemas()
    yield
    testdb.drop(A, B)


@pytest.fixture
def http(schemas):
    import server as mcp_server
    with serving(mcp_server.app()) as base:
        yield base


