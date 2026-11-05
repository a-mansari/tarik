"""Sparrow's forward of a collaborator's WhatsApp message (engine/tools/core/sparrow.py).

Sparrow POSTs each message to /sparrow/handler with the recipient's recipient_id as bearer, waits 15 s for the answer
and never sends it again: the route answers at once and the work goes on in the background. The recipient_id says
which cabinet and which collaborator the message is from (table whatsapp_recipients); a message from anyone else is
refused. The message is answered as a Discussion message is (the same graph, in the person's own conversation), by a
WhatsApp reply."""

import asyncio
import base64
import collections
import traceback

from starlette.requests import Request
from starlette.responses import JSONResponse

from engine import runs
from engine.calls import call, within
from engine.tools.core import sparrow
from engine.tools.core.app import log
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import CABINETS
from engine.tools.core.config import FILE_TYPES
from engine.tools.core.queries import Q
from engine.tools.dossiers.people import collaborator, remember_whatsapp_sender
from server.api.chat import MAX_FILE, MAX_FILES, digest, problems

ERROR = "Tarik n'a pas pu répondre pour le moment. Renvoyez votre message dans un instant."
BUSY = "Un souci technique m'a arrêté. Renvoyez votre message dans un instant."
# A person's messages are answered one after the other: two runs at once on one conversation would write its memory
# together. shortcut: a photo sent right after another is a second answer, not one for both; group them if it annoys.
LOCKS = collections.defaultdict(asyncio.Lock)
TASKS = set()  # the answers going on: held, or the loop could drop one before it ends


async def recipient_of(recipient_id):
    """Return (cabinet, email, session_id) of the enrolled person ``recipient_id`` stands for, None when unknown."""
    async with runs.pool.connection() as db:
        return await (await db.execute(Q.whatsapp_recipient, (recipient_id,))).fetchone()


async def first_time(message_id):
    """True the first time Sparrow sends message ``message_id``, False when it is sent again."""
    async with runs.pool.connection() as db:
        return await (await db.execute(Q.whatsapp_message_taken, (message_id,))).fetchone() is not None


def reply_of(output):
    """Return what to tell the person after a Discussion run: its reply, else its error, else ERROR."""
    output = output or {}
    return output.get("reply") or output.get("error") or ERROR


async def answer(cabinet, email, session, recipient_id, text, files):
    """Answer the message in the background: check the person still is a collaborator, save the files, run the
    Discussion graph in their conversation and send the reply by WhatsApp.

    Nothing is sent to someone who no longer is a collaborator (as email's refused sender). A failure tells the person
    once to send again; what Sparrow refuses (the 24 h window closed) is only logged."""
    try:
        async with LOCKS[session]:
            if not await within(cabinet, collaborator, email):
                log(f"whatsapp: {email} is no collaborator of {cabinet}: not answered")
                return
            exceeded = problems(text, files)
            skipped = []
            if not exceeded:  # a zip is replaced by the files in it Tarik takes
                found, skipped = unpacked([(f["file_name"], f["mime_type"], f["data"]) for f in files], FILE_TYPES)
                files = [{"file_name": name, "mime_type": mime, "data": data} for name, mime, data in found]
            if exceeded:
                reply = "Rien n'a été traité :\n" + "\n".join(f"- {line}" for line in exceeded)
            elif not text and not files:
                reply = "Aucun fichier utilisable dans l'archive : " + " ; ".join(skipped)
            else:
                await within(cabinet, remember_whatsapp_sender, session, email)
                if files:
                    saved = [{**f, "data": base64.b64encode(f["data"]).decode(), "id": digest(f)} for f in files]
                    await call(cabinet, "save_files", session_id=session, files=saved)
                given = {
                    "cabinet": cabinet,
                    "session_id": session,
                    "message": text,
                    "files": [f["file_name"] for f in files],
                    "skipped": skipped,
                }
                start = {"input": given, "cabinet": cabinet, "session_id": session}
                writer = runs.WRITER.set({"email": email, "admin": False})  # a collaborator, not a Tarik admin
                try:  # the run started here, and those nested in it, carry it
                    _, task = await runs.start("discussion", start)
                finally:
                    runs.WRITER.reset(writer)
                output, _ = await task
                reply = reply_of(output)
        await within(cabinet, sparrow.send, recipient_id, reply)
    except Exception as error:
        log(f"whatsapp: {cabinet} {session}: {type(error).__name__}: {error}\n{traceback.format_exc()}")
        try:
            await within(cabinet, sparrow.send, recipient_id, BUSY)
        except Exception as again:  # Sparrow itself is down or refuses (the 24 h window): nothing more to do
            log(f"whatsapp: {cabinet} {session}: not told either: {type(again).__name__}")


async def handler(request: Request):
    """POST /sparrow/handler: a WhatsApp message from Sparrow {message_id, reply_to_message_id, content, and for a file
    mimetype, filename, file (base64)}, with the recipient's recipient_id as bearer.

    401 for a recipient_id nobody is enrolled under, 400 for a message that cannot be read. Otherwise 200 at once
    ("accepted"), also for a message already taken: it is answered in the background (answer)."""
    recipient_id = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    who = await recipient_of(recipient_id) if recipient_id else None
    if who is None or who[0] not in CABINETS:
        return JSONResponse({"error": "unknown recipient"}, status_code=401)
    if int(request.headers.get("content-length") or 0) > MAX_FILES * MAX_FILE * 4 // 3 + 64 * 1024:
        return JSONResponse({"error": "message too large"}, status_code=400)
    try:
        payload = await request.json()
        message_id = str(payload["message_id"])
        text = str(payload.get("content") or "").strip()
        files = []
        if payload.get("file"):
            data = base64.b64decode(payload["file"], validate=True)
            name = str(payload.get("filename") or "").strip()
            files.append({"file_name": name, "mime_type": str(payload.get("mimetype") or ""), "data": data})
    except ValueError, KeyError, TypeError:
        return JSONResponse({"error": "unreadable message"}, status_code=400)
    if not await first_time(message_id):
        return JSONResponse({"status": "accepted"})
    task = asyncio.create_task(answer(*who, recipient_id, text, files))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return JSONResponse({"status": "accepted"})
