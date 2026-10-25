"""The files Tarik makes for a conversation (an export, a declaration's XML), kept in its outbox (tarik_outbox): sent
with its next email reply (send_email, attach_session), downloadable from the conversation in the chat."""

import base64

from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import odoo_call
from engine.tools.core.queries import Q

MIMES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "txt": "text/plain",
    "xml": "application/xml",
}


def keep_file(session_id, name, data):
    """File ``name`` (bytes ``data``) kept in the conversation's outbox."""
    mime = MIMES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    with cabinet_db() as db:
        db.execute(Q.insert_outbox, (session_id, name, mime, data))


def keep_record_file(session_id, model, record_id, field, name_field, context):
    """The file Experio stored on a record (``field``, its name in ``name_field``: a declaration's XML) kept in the
    conversation's outbox: its name, or "" when the record holds none (or there is no conversation)."""
    found = odoo_call(model, "read", [record_id], fields=[field, name_field], context=context)[0]
    if not session_id or not found[field]:
        return ""
    name = found[name_field] or f"{model}-{record_id}"
    keep_file(session_id, name, base64.b64decode(found[field]))
    return name
