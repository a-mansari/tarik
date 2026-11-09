"""The files Tarik makes for a conversation (an export, a declaration's XML), stored in its outbox (tarik_outbox):
sent with its next reply (an email's attachments: send_email, attach_session; WhatsApp's documents: send_unsent) and
downloadable from the conversation in the chat."""

import base64

from engine.tools.core import sparrow
from engine.tools.core.app import log
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import odoo_call
from engine.tools.core.queries import Q

MIMES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "txt": "text/plain",
    "csv": "text/csv",
    "xml": "application/xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "tex": "application/x-tex",
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


def unsent_files(session_id):
    """Return (id, name, mime) of each file conversation ``session_id``'s outbox holds that no reply has sent, in the
    order made: the bytes are read one file at a time (send_unsent_file)."""
    with cabinet_db() as db:
        return [tuple(row) for row in db.execute(Q.outbox_unsent_names, (session_id,))]


def send_unsent_file(session_id, recipient_id, file):
    """Send ``file`` (an unsent_files entry) to ``recipient_id`` as a WhatsApp document and mark it sent. Returns True
    when it went (or was sent meanwhile), False when it did not: it then stays in the outbox for an email reply,
    except one Sparrow will never take (422: empty, too big, name too long), marked sent so no reply tries it again.
    Sparrow's refusal is logged, never raised."""
    file_id, name, mime = file
    with cabinet_db() as db:
        row = db.execute(Q.outbox_file, (file_id, session_id)).fetchone()
    if row is None:
        return True
    sent = True
    try:
        sparrow.send_file(recipient_id, name, mime, row[0])
    except sparrow.SparrowError as error:
        log(f"whatsapp: {session_id}: file {name}: {error}")
        if error.status != 422:
            return False
        sent = False
    with cabinet_db() as db:
        db.execute(Q.mark_outbox_sent, ([file_id],))
    return sent
