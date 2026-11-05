"""The link to a new dossier's form (dossiers/form.py): a one-time address, <PUBLIC_URL>/formulaire/<cabinet>/<token>,
open FORM_DAYS days (tarik_dossier_forms), served without a login by server/api/form.py: its random token is its key,
as Gladden's callback's is."""

import secrets

from engine.tools.core.cabinets import cabinet_db, current, setting
from engine.tools.core.queries import Q

FORM_DAYS = 7


def form_link(dossier_id, session_id):
    """Make a form for dossier ``dossier_id`` (conversation ``session_id``) and return its link, or "" when this
    server's public address (PUBLIC_URL, Vault secret/tarik/shared) is not set: Tarik then asks by email."""
    base = setting("PUBLIC_URL", "").rstrip("/")
    if not base:
        return ""
    token = secrets.token_urlsafe(24)
    with cabinet_db() as db:
        db.execute(Q.form_made, (token, dossier_id, session_id, FORM_DAYS))
    return f"{base}/formulaire/{current()}/{token}"
