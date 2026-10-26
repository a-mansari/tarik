"""Who may give Tarik work: the cabinet's collaborators and Experio's own staff, never a dossier's client contacts."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import people

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


@pytest.mark.communication
def test_the_collaborators_are_listed_with_their_role_and_dossiers(monkeypatch):
    users = [
        {
            "id": 1,
            "name": "Chef",
            "login": "chef@cabinet.ma",
            "employee_state": "active",
            "exp_profile_id": [2, "Manager"],
            "is_super_manager": True,
            "company_ids": [7, 8],
        },
        {
            "id": 3,
            "name": "Sam",
            "login": "sam@cabinet.ma",
            "employee_state": False,
            "exp_profile_id": False,
            "is_super_manager": False,
            "company_ids": [],
        },
    ]
    calls = []

    def odoo_call(model, method, arg, **kw):
        calls.append((model, method, arg))
        if model == "exp.user.profile":
            return [{"id": 2, "management_group": "admin"}]
        return users if method == "search_read" else arg == [1]

    monkeypatch.setattr(people, "odoo_call", odoo_call)
    assert people.list_collaborators().splitlines() == [
        "2 collaborateur(s) :",
        "- Chef (chef@cabinet.ma) : Manager (gestion : administrateur), manager, super manager, 2 dossier(s)",
        "- Sam (sam@cabinet.ma) : sans profil, non manager, 0 dossier(s), compte inactif",
    ]
    assert ("exp.user.profile", "read", [2]) in calls
    monkeypatch.setattr(people, "odoo_call", lambda model, method, arg, **kw: [] if method == "search_read" else 1 / 0)
    assert people.list_collaborators() == "0 collaborateur(s) :\n"  # no profile to read


@pytest.mark.communication
def test_experio_not_giving_the_collaborators_is_said(monkeypatch):
    monkeypatch.setattr(people, "odoo_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(ToolError, match="Experio n'a pas pu donner la liste des collaborateurs"):
        people.list_collaborators()


@pytest.mark.security
def test_an_address_is_compared_whole_never_as_a_pattern(monkeypatch):
    asked = []
    monkeypatch.setattr(people, "odoo_call", lambda model, method, domain, **kw: asked.append(domain) or [])
    people.users_by_email("jean%_\\@cabinet.ma", ["employee"], ["id"], 1)
    assert asked[0][2:] == [
        ("login", "=ilike", "jean\\%\\_\\\\@cabinet.ma"),
        ("email", "=ilike", "jean\\%\\_\\\\@cabinet.ma"),
    ]


@pytest.mark.security
def test_two_users_with_one_address_are_not_a_reliable_sender(monkeypatch):
    monkeypatch.setattr(people, "odoo_call", lambda *a, **k: [{"name": "A"}, {"name": "B"}])
    assert people.collaborator("x@cabinet.ma") is None


@pytest.fixture
def attaching(monkeypatch):
    """Experio's calls putting a user on dossier 9; ``profiled``: their profile row there already."""
    state = {"calls": [], "profiled": 0}

    def odoo_call(model, method, *args, **kw):
        state["calls"].append((model, method, args))
        if method == "read":
            return [{"exp_profile_id": [4, "Robot"]}]
        return state["profiled"] if method == "search_count" else True

    monkeypatch.setattr(people, "odoo_call", odoo_call)
    monkeypatch.setattr(people, "login", lambda: 66)
    return state


@pytest.mark.functionality
def test_tarik_puts_itself_on_a_dossier_it_made_with_its_own_profile(attaching):
    people.attach_user(None, 9)
    calls = attaching["calls"]
    assert calls[0] == ("res.users", "read", ([66],))
    assert calls[1] == ("res.company", "write", ([9], {"employee_ids": [(4, 66)]}))
    assert calls[-1] == ("exp.user.company.profile", "create", ({"user_id": 66, "company_id": 9, "profile_id": 4},))


@pytest.mark.functionality
def test_a_client_goes_in_the_contacts_and_an_existing_profile_row_is_kept(attaching):
    attaching["profiled"] = 1
    people.attach_user(42, 9, 3, "client")
    assert attaching["calls"][0] == ("res.company", "write", ([9], {"contact_ids": [(4, 42)]}))
    assert [method for _, method, _ in attaching["calls"]] == ["write", "search_count"]


@pytest.mark.functionality
def test_a_user_without_a_profile_gets_no_profile_row(attaching, monkeypatch):
    profiled = people.odoo_call
    monkeypatch.setattr(
        people,
        "odoo_call",
        lambda m, method, *a, **k: [{"exp_profile_id": False}] if method == "read" else profiled(m, method, *a, **k),
    )
    people.attach_user(None, 9)
    assert [method for _, method, _ in attaching["calls"]] == ["write"]


@pytest.mark.functionality
def test_the_sender_is_the_email_threads_client(db_cabinet):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_mail_threads (thread_id, session_id, client_address, subject) "
            "values ('<1@x>', 'mail', 'chef@cabinet.ma', 'FEC')"
        )
    assert people.sender_of("mail") == "chef@cabinet.ma" and people.sender_of("chat") is None


@pytest.mark.security
def test_work_on_an_invoice_is_checked_against_its_dossier(monkeypatch):
    monkeypatch.setattr(people, "require_invoice", lambda invoice_id, fields: {"company_id": [9, "BETA"]})
    monkeypatch.setattr(people, "require_dossier", lambda dossier_id: {9: "BETA"}[dossier_id])
    monkeypatch.setattr(people, "sender_of", lambda session_id: "x@cabinet.ma")
    monkeypatch.setattr(
        people, "collaborator", lambda address: {"name": "Sam", "employee_type": "employee", "company_ids": [7]}
    )
    answer = people.check_sender("s", invoice_id=31)
    assert answer["status"] == "stop" and "Le dossier BETA n'est pas attribue a Sam" in answer["message"]
    monkeypatch.setattr(people, "sender_of", lambda session_id: None)
    assert people.check_sender("s", invoice_id=31)["routine"]  # the chat: no sender to check
