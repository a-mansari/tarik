"""Two throwaway cabinets (shared.A, shared.B) in the test database (tests/testdb.py): the fixtures the cabinet
isolation tests share."""

import pytest

from shared import A, B
from tarik.core import cabinets
from tests import testdb


@pytest.fixture
def two_cabinets(monkeypatch):
    names = frozenset({A, B})
    monkeypatch.setattr(cabinets, "CABINETS", names)
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
