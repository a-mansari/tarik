"""The cabinet's people as Experio knows them, and who may have work done on which dossier.

Experio has no collaborator model: people are res.users with employee_type "employee" (the cabinet's
collaborators), "internal" (Experio's own staff, who see every active dossier) or "client" (a dossier's
contacts). A user's role is their profile (exp_profile_id: "Collaborateur", "Chef de mission", "Manager"...),
whose management_group is none, user, approver or admin; Experio's own res.users user_is_manager() says who
manages (never a client). A user's dossiers are their company_ids, which Experio keeps in step with each
dossier's employee_ids and contact_ids (res.company and res.users write): the same dossiers they see in Experio.
"""

import re

from engine.tools.core.app import routine, stop, tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import DOSSIER_DOMAIN
from engine.tools.core.experio import login, odoo_call, refused, require_dossier, require_invoice
from engine.tools.core.queries import Q

# Who may give Tarik work: the cabinet's collaborators and Experio's own staff (not client contacts).
STAFF = ["employee", "internal"]
LEVELS = {"none": "aucune", "user": "utilisateur", "approver": "approbateur", "admin": "administrateur"}
USER_FIELDS = ["name", "login", "employee_state", "exp_profile_id", "is_super_manager", "company_ids"]


def collaborator_line(user, levels, manager):
    """Return one French line on ``user`` (res.users read with USER_FIELDS): role, manager or not, number of
    dossiers, account state.

    Args:
        levels: {profile id: management_group} of the users' profiles.
        manager: Experio's user_is_manager() for this user.
    """
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
    """Return, as French text, the cabinet's collaborators (employee_type "employee") in Experio: each one's
    role (profile and management level), whether Experio counts them as a manager, and how many dossiers are
    assigned to them."""
    with refused("list_collaborators", "Experio n'a pas pu donner la liste des collaborateurs."):
        users = odoo_call(
            "res.users", "search_read", [("employee_type", "=", "employee")], order="name", fields=USER_FIELDS
        )
        profile_ids = sorted({user["exp_profile_id"][0] for user in users if user["exp_profile_id"]})
        profiles = []
        if profile_ids:
            profiles = odoo_call("exp.user.profile", "read", profile_ids, fields=["management_group"])
        levels = {profile["id"]: profile["management_group"] or "none" for profile in profiles}  # "none", "user"...
        managers = {user["id"]: odoo_call("res.users", "user_is_manager", [user["id"]]) for user in users}
    lines = [collaborator_line(user, levels, managers[user["id"]]) for user in users]
    return f"{len(users)} collaborateur(s) :\n" + "\n".join(lines)


def users_by_email(address, employee_types, fields, limit):
    """Return the res.users (at most ``limit``, read with ``fields``) of ``employee_types`` whose login or email is
    ``address``, compared whole and case-insensitively. %, _ and backslash are escaped, since =ilike would read
    them as wildcards (jean%@cabinet.ma matching jean.dupont@cabinet.ma)."""
    exact = re.sub(r"([\\%_])", r"\\\1", address)  # a backslash, % or _: escaped with a backslash
    return odoo_call(
        "res.users",
        "search_read",
        [("employee_type", "in", employee_types), "|", ("login", "=ilike", exact), ("email", "=ilike", exact)],
        fields=fields,
        limit=limit,
    )


def collaborator(address):
    """Return the Experio collaborator or staff member (STAFF) whose login or email is ``address``, as {id, name,
    company_ids, employee_type, employee_state}; None for an unknown address, a client contact, a deactivated user
    (active False, Experio's action_deactivate_user) or an address two users share."""
    found = users_by_email(address, STAFF, ["name", "company_ids", "employee_type", "employee_state"], 2)
    return found[0] if len(found) == 1 else None  # two users with one address: not a reliable sender


def scoped(user, dossier_ids):
    """Return ``user`` (collaborator's) limited to the dossiers of ``dossier_ids`` it may still see: theirs in
    Experio (company_ids; every one for Experio's staff) and still open (DOSSIER_DOMAIN: a dossier reset to new stays
    in company_ids, res_company.py action_reset_to_new). Staff so limited are checked as a collaborator is.

    Returns None for no user, or a collaborator whose account Experio does not count as live (employee_state not
    « active »: invited and never in, or a copy)."""
    if not user or (user["employee_type"] != "internal" and user.get("employee_state") != "active"):
        return None
    mine = [d for d in dossier_ids if user["employee_type"] == "internal" or d in user["company_ids"]]
    found = set(odoo_call("res.company", "search", [*DOSSIER_DOMAIN, ("id", "in", mine)])) if mine else set()
    return {**user, "employee_type": "employee", "company_ids": [d for d in mine if d in found]}


def sender_user(session_id):
    """Return (address, user) for the conversation's sender: (None, None) in the chat; the address and its
    collaborator (None if none). A routine's run is its report address limited to the routine's dossiers (scoped):
    its instruction never reaches another dossier, whatever the address may see."""
    address = sender_of(session_id)
    if address is None:
        return None, None
    user = collaborator(address) if address else None
    if session_id.startswith(ROUTINE_SESSION):
        with cabinet_db() as db:
            row = db.execute(Q.routine_dossiers, (routine_of_session(session_id),)).fetchone()
        user = scoped(user, row[0] if row else [])
    return address, user


@tool
def check_creator(session_id: str) -> dict:
    """Check, before dossiers are created (one, or a spreadsheet's list), that the email's sender is a cabinet
    manager (Experio's own user_is_manager) or Experio staff.

    Returns a routine result when allowed or in the chat (no sender to check); a stop result, reported to the
    client, when the sender is unknown or not a manager: nothing is created."""
    address, user = sender_user(session_id)
    if address is None:
        return routine("Conversation sans expediteur a verifier.")
    if not user:
        return stop(f"`{address}` n'est ni collaborateur du cabinet ni de l'equipe Experio : aucun dossier n'est cree.")
    if user["employee_type"] == "internal":
        return routine(f"`{user['name']}` (equipe Experio) peut creer des dossiers.")
    if odoo_call("res.users", "user_is_manager", [user["id"]]):
        return routine(f"`{user['name']}` (manager du cabinet) peut creer des dossiers.")
    return stop(
        f"Seuls les managers du cabinet et l'equipe Experio creent des dossiers ; `{user['name']}` n'en fait pas "
        "partie : aucun dossier n'est cree. Demandez a un manager du cabinet."
    )


# Who may manage people in Experio: users and dossiers' members are written by group_management_approver and above
# (base_client/security/ir.model.access.csv lines 9 and 13), the profile's management_group approver or admin; a
# dossier's profiles (exp.user.company.profile) by admin only (line 23).
RANK = {"none": 0, "user": 1, "approver": 2, "admin": 3}


def level_of(profile):
    """Return the management_group of ``profile`` ([id, name] or id; False for none): "none" when it has none."""
    if not profile:
        return "none"
    profile_id = profile[0] if isinstance(profile, list) else profile
    found = odoo_call("exp.user.profile", "read", [profile_id], fields=["management_group"])
    return (found[0]["management_group"] if found else None) or "none"


def sender_reach(session_id):
    """Return how far the email's sender may manage people: None for no limit (the chat, where nobody can tell who
    writes, and Experio's staff); else {id, name, company_ids, rank} of the collaborator (rank: RANK of their
    profile's management_group), or {} when the sender is no collaborator.

    Tarik writes with its own wide rights, while Experio no longer limits a manager to their dossiers (its
    res_company_rule_employee is deleted, base_client/security/base_security.xml:31-33): the access tools keep a
    sender to their own dossiers and to people at or below their own level."""
    address, user = sender_user(session_id)
    if address is None:
        return None
    if not user:
        return {}
    if user["employee_type"] == "internal":
        return None
    profile = odoo_call("res.users", "read", [user["id"]], fields=["exp_profile_id"])[0]["exp_profile_id"]
    return {**user, "rank": RANK[level_of(profile)]}


@tool
def check_manager(session_id: str) -> dict:
    """Check, before people are created, invited, given or taken a dossier or another role, that the email's sender
    is Experio staff or a collaborator whose own Experio profile may do it (management_group approver or admin):
    Tarik never does for someone what Experio would refuse them. Which dossiers and people the sender may touch is
    checked by each tool (sender_reach).

    Returns a routine result when allowed or in the chat (no sender to check); a stop result, reported to the
    client, when the sender is unknown or their profile may not: nothing is changed."""
    reach = sender_reach(session_id)
    if reach is None:
        return routine("Expediteur de l'equipe Experio, ou conversation sans expediteur a verifier.")
    if not reach:
        address = sender_of(session_id)
        return stop(f"`{address}` n'est ni collaborateur du cabinet ni de l'equipe Experio : aucun acces n'est change.")
    if reach["rank"] >= RANK["approver"]:
        return routine(f"`{reach['name']}` peut gerer les acces de ses dossiers.")
    return stop(
        "Seuls les managers du cabinet dont le profil Experio gere les utilisateurs (approbateur ou administrateur) "
        f"et l'equipe Experio gerent les acces ; `{reach['name']}` n'en fait pas partie : rien n'est change. "
        "Demandez a un manager du cabinet."
    )


def attach_user(user_id, dossier_id, profile_id=None, employee_type="employee"):
    """Put a user on a dossier with a profile there, as the dossier form does.

    Adds the user to the dossier's Collaborateurs (employee_ids) for ``employee_type`` "employee", else to its
    Clients (contact_ids); Experio's res.company write copies that into the user's company_ids. Then creates the
    "Profils par Dossier" row (exp.user.company.profile) unless one exists or ``profile_id`` is empty.

    Args:
        user_id: the res.users id; None for Tarik's own user, with its own profile, so Tarik sees the dossier it
            just created.
        profile_id: the exp.user.profile id to give on this dossier.
    """
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


ROUTINE_SESSION = "routine-"  # a routine's run: routine-<id>-<period> (engine/tools/dossiers/routines.py)


def routine_of_session(session_id):
    """Return the routine id of a routine's run session (``ROUTINE_SESSION<id>-<period>``), 0 when unreadable."""
    routine_id = session_id.removeprefix(ROUTINE_SESSION).split("-")[0]
    return int(routine_id) if routine_id.isdigit() else 0


def sender_of(session_id):
    """Return the sender's address of an email conversation, the routine's report address for a routine's run (its
    instruction runs with that person's rights); None in the chat, where nobody can tell who writes."""
    with cabinet_db() as db:
        if session_id.startswith(ROUTINE_SESSION):
            row = db.execute(Q.routine_notify, (routine_of_session(session_id),)).fetchone()
            return row[0] if row else ""  # a routine gone: an address nobody has, so nothing is allowed
        row = db.execute(Q.sender_of_session, (session_id,)).fetchone()
    return row[0] if row else None


@tool
def check_sender(session_id: str, dossier_id: int = 0, invoice_id: int = 0) -> dict:
    """Check, before work on an existing dossier (``dossier_id``, or the dossier of ``invoice_id``), that the
    email's sender is Experio staff or a collaborator with that dossier in their company_ids.

    Returns a routine result when allowed or in the chat (no sender to check); a stop result, reported to the
    client, when the sender is unknown or the dossier isn't assigned to them."""
    if invoice_id and not dossier_id:
        dossier_id = require_invoice(invoice_id, ["company_id"])["company_id"][0]
    dossier = require_dossier(dossier_id)
    address, user = sender_user(session_id)
    if address is None:
        return routine("Conversation sans expediteur a verifier.")
    if not user:
        return stop(
            f"`{address}` n'est ni collaborateur du cabinet ni de l'equipe Experio : aucun travail sur le dossier "
            f"`{dossier}`."
        )
    if user["employee_type"] == "internal":
        return routine(f"`{user['name']}` (equipe Experio) peut travailler sur le dossier `{dossier}`.")
    if dossier_id not in user["company_ids"]:
        return stop(f"Le dossier `{dossier}` n'est pas attribue a `{user['name']}` dans Experio : rien n'y est fait.")
    return routine(f"`{user['name']}` a le dossier `{dossier}`.")
