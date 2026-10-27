"""The files Tarik makes for a conversation (an export, a declaration's XML), stored in its outbox (tarik_outbox):
sent with its next email reply (send_email, attach_session) and downloadable from the conversation in the chat."""

import base64

from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import odoo_call
from engine.tools.core.queries import Q

MIMES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "txt": "text/plain",
    "csv": "text/csv",
    "xml": "application/xml",
}


def keep_file(session_id, name, data):
    """Store file ``name`` (bytes ``data``) in conversation ``session_id``'s outbox, typed by its extension."""
    mime = MIMES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    with cabinet_db() as db:
        db.execute(Q.insert_outbox, (session_id, name, mime, data))


def keep_record_file(session_id, model, record_id, field, name_field, context):
    """Store the file Experio keeps on a record (a declaration's XML) in conversation ``session_id``'s outbox.

    Args:
        model, record_id: the Experio record.
        field: the binary field holding the file (base64); name_field: the field holding its name.
        context: the Experio context to read it in.

    Returns the file's name (<model>-<record_id> when the record names none); "" when the field is empty or there
    is no conversation."""
    found = odoo_call(model, "read", [record_id], fields=[field, name_field], context=context)[0]
    if not session_id or not found[field]:
        return ""
    name = found[name_field] or f"{model}-{record_id}"
    keep_file(session_id, name, base64.b64decode(found[field]))
    return name
