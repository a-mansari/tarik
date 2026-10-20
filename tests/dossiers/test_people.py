"""Who may give Tarik work: the cabinet's collaborators and Experio's own staff, never a dossier's client contacts."""

import pytest

from tarik.dossiers import people

USERS = [
    {"login": "chef@cabinet.ma", "email": "", "employee_type": "employee", "name": "Chef", "company_ids": [7]},
    {"login": "jane-doe", "email": "jane@experio.ma", "employee_type": "internal", "name": "Jane", "company_ids": [7]},
    {"login": "client@societe.ma", "email": "", "employee_type": "client", "name": "Client", "company_ids": [7]},
]


@pytest.mark.security
def test_collaborators_and_experio_staff_give_work_never_client_contacts(monkeypatch):
    def odoo_call(model, method, domain, fields, limit):
        types, address = domain[0][2], domain[3][2]
        return [user for user in USERS if user["employee_type"] in types and address in (user["login"], user["email"])]

    monkeypatch.setattr(people, "odoo_call", odoo_call)
    assert people.collaborator("chef@cabinet.ma")["name"] == "Chef"
    assert people.collaborator("jane@experio.ma")["name"] == "Jane"  # staff: Experio's "internal" users
    assert people.collaborator("client@societe.ma") is None
