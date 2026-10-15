"""The cabinet's people, as Experio knows them, and who may have work done on which dossier.

Experio has no collaborator model: they are res.users with employee_type "employee" (the
cabinet) or "client" (a dossier's contacts). A user's role is their profile (exp_profile_id:
"Collaborateur", "Chef de mission", "Manager"...), whose management level is none, user,
approver or admin; Experio's own user_is_manager() says who manages. A user's dossiers are
their company_ids, which Experio keeps in step with each dossier's Employees and Contacts:
the same dossiers they see in Experio.
"""

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server, stop
from tarik.core.experio import POSTGRES, odoo_call, require_dossier

LEVELS = {"none": "aucune", "user": "utilisateur", "approver": "approbateur", "admin": "administrateur"}


def management_levels(profile_ids):
    profiles = odoo_call("exp.user.profile", "read", sorted(profile_ids), fields=["management_group"]) if profile_ids else []
    return {profile["id"]: profile["management_group"] or "none" for profile in profiles}


@server.tool()
def list_collaborators() -> str:
    """The cabinet's collaborators in Experio: their role (profile and management level),
    whether Experio counts them as managers, and how many dossiers are assigned to them."""
    try:
        users = odoo_call("res.users", "search_read", [("employee_type", "=", "employee")], order="name",
                          fields=["name", "login", "employee_state", "exp_profile_id", "is_super_manager", "company_ids"])
        levels = management_levels({user["exp_profile_id"][0] for user in users if user["exp_profile_id"]})
        managers = {user["id"]: odoo_call("res.users", "user_is_manager", [user["id"]]) for user in users}
    except Exception as error:
        print(f"list_collaborators: {error}", flush=True)
        raise ToolError("Experio n'a pas pu donner la liste des collaborateurs.") from None
    lines = []
    for user in users:
        profile = user["exp_profile_id"]
        role = (f"{profile[1]} (gestion : {LEVELS[levels.get(profile[0], 'none')]})" if profile else "sans profil")
        lines.append(f"- {user['name']} ({user['login']}) : {role}, {'manager' if managers[user['id']] else 'non manager'}"
                     f"{', super manager' if user['is_super_manager'] else ''}, {len(user['company_ids'])} dossier(s)"
                     f"{'' if user['employee_state'] == 'active' else ', compte ' + (user['employee_state'] or 'inactif')}")
    return f"{len(users)} collaborateur(s) :\n" + "\n".join(lines)


def collaborator(address):
    """The active Experio collaborator with this email (their login), or None: client contacts
    and unknown addresses don't give Tarik work."""
    found = odoo_call("res.users", "search_read", [("employee_type", "=", "employee"),
                                                   "|", ("login", "=ilike", address), ("email", "=ilike", address)],
                      fields=["name", "company_ids"], limit=2)
    return found[0] if len(found) == 1 else None  # two users with one address: not a reliable sender


def sender_of(session_id):
    """Who wrote, for an email conversation; None in the chat, where Experio can't tell who writes."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select client_address from tarik_mail_threads where session_id = %s", (session_id,)).fetchone()
    return row[0] if row else None


@server.tool()
def check_sender(session_id: str, dossier_id: int = 0, invoice_id: int = 0) -> dict:
    """Before work on an existing dossier (``dossier_id``, or the dossier of ``invoice_id``):
    the email's sender must be an Experio collaborator with that dossier assigned to them.
    The chat has no sender to check."""
    if invoice_id and not dossier_id:
        found = odoo_call("exp.invoice", "read", [invoice_id], fields=["company_id"])
        dossier_id = found[0]["company_id"][0] if found and found[0]["company_id"] else 0
    dossier = require_dossier(dossier_id)
    address = sender_of(session_id)
    if address is None:
        return ok("Conversation sans expediteur a verifier.")
    user = collaborator(address)
    if not user:
        return stop(f"{address} n'est pas un collaborateur du cabinet dans Experio : aucun travail sur le dossier {dossier}.")
    if dossier_id not in user["company_ids"]:
        return stop(f"Le dossier {dossier} n'est pas attribue a {user['name']} dans Experio : rien n'y est fait.")
    return ok(f"{user['name']} a le dossier {dossier}.")
