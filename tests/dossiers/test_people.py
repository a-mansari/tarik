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


@pytest.mark.security
@pytest.mark.parametrize(
    "who, manager, allowed",
    [
        ({"id": 1, "name": "Chef", "employee_type": "employee"}, True, True),  # the cabinet's manager
        ({"id": 2, "name": "Jane", "employee_type": "internal"}, False, True),  # Experio's staff
        ({"id": 3, "name": "Sam", "employee_type": "employee"}, False, False),  # a collaborator, not a manager
        (None, False, False),  # nobody Experio knows
    ],
)
def test_only_managers_and_experio_staff_create_dossiers(monkeypatch, who, manager, allowed):
    monkeypatch.setattr(people, "sender_of", lambda session_id: "x@cabinet.ma")
    monkeypatch.setattr(people, "collaborator", lambda address: who)
    monkeypatch.setattr(people, "odoo_call", lambda model, method, ids: manager)
    assert (people.check_creator("s")["status"] == "ok") is allowed


@pytest.mark.functionality
def test_the_chat_has_no_sender_to_check(monkeypatch):
    monkeypatch.setattr(people, "sender_of", lambda session_id: None)
    assert people.check_creator("s")["status"] == "ok"


@pytest.mark.security
@pytest.mark.parametrize(
    "who, allowed",
    [
        ({"name": "Chef", "employee_type": "employee", "company_ids": [7]}, True),  # the dossier is theirs
        ({"name": "Sam", "employee_type": "employee", "company_ids": [9]}, False),  # a collaborator, not on it
        ({"name": "Jane", "employee_type": "internal", "company_ids": []}, True),  # Experio's staff: every dossier
        (None, False),  # nobody Experio knows
    ],
)
def test_only_collaborators_on_the_dossier_and_experio_staff_work_on_it(monkeypatch, who, allowed):
    monkeypatch.setattr(people, "require_dossier", lambda dossier_id: "SOCIETE")
    monkeypatch.setattr(people, "sender_of", lambda session_id: "x@cabinet.ma")
    monkeypatch.setattr(people, "collaborator", lambda address: who)
    assert (people.check_sender("s", dossier_id=7)["status"] == "ok") is allowed
