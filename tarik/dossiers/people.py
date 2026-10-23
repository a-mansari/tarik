"""The cabinet's people, as Experio knows them, and who may have work done on which dossier.

Experio has no collaborator model: they are res.users with employee_type "employee" (the
cabinet), "internal" (Experio's own staff, given every dossier of every cabinet) or "client" (a
dossier's contacts). A user's role is their profile (exp_profile_id:
"Collaborateur", "Chef de mission", "Manager"...), whose management level is none, user,
approver or admin; Experio's own user_is_manager() says who manages. A user's dossiers are
their company_ids, which Experio keeps in step with each dossier's Employees and Contacts:
the same dossiers they see in Experio.
"""

import re

from tarik.core.app import routine, stop, tool
from tarik.core.cabinets import cabinet_db
from tarik.core.experio import login, odoo_call, refused, require_dossier, require_invoice
from tarik.core.queries import Q

# Who may give Tarik work: the cabinet's collaborators and Experio's own staff (not client contacts).
STAFF = ["employee", "internal"]
LEVELS = {"none": "aucune", "user": "utilisateur", "approver": "approbateur", "admin": "administrateur"}
USER_FIELDS = ["name", "login", "employee_state", "exp_profile_id", "is_super_manager", "company_ids"]


def collaborator_line(user, levels, manager):
    """One line on ``user`` (res.users, USER_FIELDS): role, manager or not (``manager``), dossiers, account state.
    ``levels`` is management_levels() of the users' profiles."""
    profile = user["exp_profile_id"]
    role = f"{profile[1]} (gestion : {LEVELS[levels.get(profile[0], 'none')]})" if profile else "sans profil"
    super_manager = ", super manager" if user["is_super_manager"] else ""
    state = "" if user["employee_state"] == "active" else ", compte " + (user["employee_state"] or "inactif")
    return (
        f"- {user['name']} ({user['login']}) : {role}, {'manager' if manager else 'non manager'}"
        f"{super_manager}, {len(user['company_ids'])} dossier(s){state}"
    )


@tool
def list_collaborators() -> str:
    """The cabinet's collaborators in Experio: their role (profile and management level),
    whether Experio counts them as managers, and how many dossiers are assigned to them."""
    with refused("list_collaborators", "Experio n'a pas pu donner la liste des collaborateurs."):
        users = odoo_call(
            "res.users", "search_read", [("employee_type", "=", "employee")], order="name", fields=USER_FIELDS
        )
        profile_ids = sorted({user["exp_profile_id"][0] for user in users if user["exp_profile_id"]})
        profiles = profile_ids and odoo_call("exp.user.profile", "read", profile_ids, fields=["management_group"])
        levels = {profile["id"]: profile["management_group"] or "none" for profile in profiles}  # "none", "user"...
        managers = {user["id"]: odoo_call("res.users", "user_is_manager", [user["id"]]) for user in users}
    lines = [collaborator_line(user, levels, managers[user["id"]]) for user in users]
    return f"{len(users)} collaborateur(s) :\n" + "\n".join(lines)


def users_by_email(address, employee_types, fields, limit):
    """The res.users of ``employee_types`` whose login or email is ``address`` (at most ``limit``, their ``fields``).
    Compared whole, case aside: Experio's =ilike would read % and _ in an address as wildcards (jean%@cabinet.ma
    matching jean.dupont@cabinet.ma)."""
    exact = re.sub(r"([\\%_])", r"\\\1", address)  # a backslash, % or _: escaped with a backslash
    return odoo_call(
        "res.users",
        "search_read",
        [("employee_type", "in", employee_types), "|", ("login", "=ilike", exact), ("email", "=ilike", exact)],
        fields=fields,
        limit=limit,
    )


def collaborator(address):
    """The active Experio collaborator or staff member (STAFF) with this email (their login), or None: client
    contacts and unknown addresses don't give Tarik work."""
    found = users_by_email(address, STAFF, ["name", "company_ids", "employee_type"], 2)
    return found[0] if len(found) == 1 else None  # two users with one address: not a reliable sender


@tool
def check_creator(session_id: str) -> dict:
    """Before dossiers are created (one, or a spreadsheet's list): the email's sender must be one of the cabinet's
    managers (Experio's own user_is_manager) or Experio's staff. Any other collaborator is told so, and nothing is
    created. The chat has no sender to check."""
    address = sender_of(session_id)
    if address is None:
        return routine("Conversation sans expediteur a verifier.")
    user = collaborator(address)
    if not user:
        return stop(f"{address} n'est ni collaborateur du cabinet ni de l'equipe Experio : aucun dossier n'est cree.")
    if user["employee_type"] == "internal":
        return routine(f"{user['name']} (equipe Experio) peut creer des dossiers.")
    if odoo_call("res.users", "user_is_manager", [user["id"]]):
        return routine(f"{user['name']} (manager du cabinet) peut creer des dossiers.")
    return stop(
        f"Seuls les managers du cabinet et l'equipe Experio creent des dossiers ; {user['name']} n'en fait pas "
        "partie : aucun dossier n'est cree. Demandez a un manager du cabinet."
    )


def attach_user(user_id, dossier_id, profile_id=None, employee_type="employee"):
    """Put a user on a dossier, with a profile there, as the dossier form does: in its Collaborateurs
    (employee_ids) or Clients (contact_ids), which Experio copies into the user's dossiers, and the
    "Profils par Dossier" row (exp.user.company.profile). Already on it: left as is. ``user_id`` None: Tarik's
    own user, with its own profile, so Tarik sees the dossier it just made."""
    if user_id is None:
        user_id = login()
        profile_id = odoo_call("res.users", "read", [user_id], fields=["exp_profile_id"])[0]["exp_profile_id"]
        profile_id = profile_id and profile_id[0]
    members = "employee_ids" if employee_type == "employee" else "contact_ids"
    odoo_call("res.company", "write", [dossier_id], {members: [(4, user_id)]})
    row = [("user_id", "=", user_id), ("company_id", "=", dossier_id)]
    if profile_id and not odoo_call("exp.user.company.profile", "search_count", row):
        odoo_call(
            "exp.user.company.profile",
            "create",
            {"user_id": user_id, "company_id": dossier_id, "profile_id": profile_id},
        )


def sender_of(session_id):
    """Who wrote, for an email conversation; None in the chat, where Experio can't tell who writes."""
    with cabinet_db() as db:
        row = db.execute(Q.sender_of_session, (session_id,)).fetchone()
    return row[0] if row else None


@tool
def check_sender(session_id: str, dossier_id: int = 0, invoice_id: int = 0) -> dict:
    """Before work on an existing dossier (``dossier_id``, or the dossier of ``invoice_id``):
    the email's sender must be Experio staff, or a collaborator with that dossier assigned to them.
    The chat has no sender to check."""
    if invoice_id and not dossier_id:
        dossier_id = require_invoice(invoice_id, ["company_id"])["company_id"][0]
    dossier = require_dossier(dossier_id)
    address = sender_of(session_id)
    if address is None:
        return routine("Conversation sans expediteur a verifier.")
    user = collaborator(address)
    if not user:
        return stop(
            f"{address} n'est ni collaborateur du cabinet ni de l'equipe Experio : aucun travail sur le dossier "
            f"{dossier}."
        )
    if user["employee_type"] == "internal":
        return routine(f"{user['name']} (equipe Experio) peut travailler sur le dossier {dossier}.")
    if dossier_id not in user["company_ids"]:
        return stop(f"Le dossier {dossier} n'est pas attribue a {user['name']} dans Experio : rien n'y est fait.")
    return routine(f"{user['name']} a le dossier {dossier}.")
