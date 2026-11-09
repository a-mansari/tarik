"""Sparrow's forward of a collaborator's WhatsApp message (engine/tools/core/sparrow.py).

Sparrow POSTs each message to /sparrow/handler with the recipient's recipient_id as bearer, waits 15 s for the answer
and never sends it again, whatever the answer: a refusal here is final, so the person is told in a WhatsApp message and
the route answers 200. The recipient_id says which cabinet and which collaborator the message is from (table
whatsapp_recipients); a message from anyone else is refused (401). The message is answered as a Discussion message is
(the same graph, in the person's own conversation), by a WhatsApp reply. Sparrow sends the text (content) and at most
one file (mimetype, filename, file in base64) per message, so what a person sends close together (a text and its
photos, the pages of an invoice) is gathered and answered as one message (gather); a reply_to_message_id comes too
and is not used: the conversation's memory carries the context.

shortcut: a message taken (whatsapp_messages) and not yet answered when the server restarts is lost, the person
sends it again; no retry store until that is seen."""

import asyncio
import base64
import collections
import json
import mimetypes
import time
import traceback

from starlette.requests import Request
from starlette.responses import JSONResponse

from engine import runs
from engine.calls import call, limited
from engine.tools.core import sparrow
from engine.tools.core.app import log
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import CABINETS
from engine.tools.core.config import FILE_TYPES
from engine.tools.core.queries import Q
from engine.tools.dossiers.people import collaborator, remember_whatsapp_sender
from server.api.chat import ACCEPTED, MAX_FILE, MAX_FILES, MAX_TEXT, digest, megabytes, problems

ERROR = "Tarik n'a pas pu répondre pour le moment. Renvoyez votre message dans un instant."
BUSY = "Un souci technique m'a arrêté. Renvoyez votre message dans un instant."
STOPPED = "Votre demande a été interrompue. Renvoyez votre message pour la reprendre."
TOO_LARGE = f"Message trop lourd : un fichier de {megabytes(MAX_FILE)} au plus par message. Envoyez-le compressé."
UNREADABLE = "Je n'ai pas pu lire votre message. Renvoyez-le."
BYTES = 3 * MAX_FILE  # what one gathered message may hold in files: its pages, not a whole folder
TOO_MUCH = (
    f"Trop de contenu d'un coup : {MAX_FILES} fichiers, {megabytes(BYTES)} et {MAX_TEXT} caractères au plus. "
    "Ce qui dépasse est laissé de côté : envoyez-le dans un second message."
)
CAP = MAX_FILE * 4 // 3 + 64 * 1024  # bytes a message may weigh: one file in base64, and its words
MAX_ID = 128  # characters of a WhatsApp message id
# A person's messages are answered one after the other: two runs at once on one conversation would write its memory
# together. Messages sent close together are one message: a text and its photos, the pages of an invoice sent as
# photos (WhatsApp sends each as a message of its own). The answer waits until the person has been quiet for
# TEXT_QUIET seconds, FILE_QUIET once a file is among them (more may follow), and never past MAX_WAIT seconds in all.
# What is gathered is capped (MAX_FILES, BYTES, MAX_TEXT): it stays in memory while the previous answer goes on.
TEXT_QUIET, FILE_QUIET, MAX_WAIT = 3, 10, 30
LOCKS = collections.defaultdict(asyncio.Lock)
BUFFERS = {}  # session -> {texts, files, bytes, chars, first, deadline, refused}: the messages being gathered
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


async def body_of(request):
    """Return the request's body, None when it weighs more than CAP: counted as it arrives, not by its header."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > CAP:
            return None
    return bytes(body)


def named(name, mime):
    """Return the file's name with the extension its mimetype gives when the name has none Tarik takes, as Experio's own
    handler does (a document WhatsApp names "Facture mars" is "Facture mars.pdf")."""
    name = name.strip()
    extension = mimetypes.guess_extension(mime) or ""
    return name if name.lower().endswith(ACCEPTED) or extension not in ACCEPTED else name + extension


def launch(work):
    """Run coroutine ``work`` in the background, held in TASKS until it ends."""
    task = asyncio.create_task(work)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def gather(cabinet, email, session, recipient_id, text, files):
    """Add a message to those being gathered for ``session``; the first one starts the wait (answer_gathered), and each
    later one extends it (never past MAX_WAIT since the first).

    What would take the gathered message past MAX_FILES files, BYTES or MAX_TEXT characters is dropped, and the person
    told so, once: it is never held in memory."""
    now = time.monotonic()
    buffer = BUFFERS.get(session)
    if buffer is None:
        BUFFERS[session] = buffer = {"texts": [], "files": [], "bytes": 0, "chars": 0, "first": now, "refused": False}
        launch(answer_gathered(cabinet, email, session, recipient_id))
    size = sum(len(file["data"]) for file in files)
    if (
        len(buffer["files"]) + len(files) > MAX_FILES
        or buffer["bytes"] + size > BYTES
        or buffer["chars"] + len(text) > MAX_TEXT
    ):
        if not buffer["refused"]:
            buffer["refused"] = True
            launch(tell(cabinet, recipient_id, TOO_MUCH))
    else:
        buffer["files"] += files
        buffer["bytes"] += size
        buffer["chars"] += len(text)
        if text:
            buffer["texts"].append(text)
    quiet = FILE_QUIET if buffer["files"] else TEXT_QUIET
    buffer["deadline"] = min(now + quiet, buffer["first"] + MAX_WAIT)


async def answer_gathered(cabinet, email, session, recipient_id):
    """Wait until the person has been quiet long enough (TEXT_QUIET, FILE_QUIET; at most MAX_WAIT since the first
    message), then for their turn (the answer to what they sent before goes first), and answer everything they sent by
    then as one message: the texts one per line, the files together. What they send while Tarik still answers the
    last message joins this one."""
    buffer = BUFFERS[session]
    try:
        while (wait := buffer["deadline"] - time.monotonic()) > 0:
            await asyncio.sleep(wait)
        async with LOCKS[session]:
            del BUFFERS[session]  # a message from now on is the next group's
            await answer(cabinet, email, session, recipient_id, "\n".join(buffer["texts"]), buffer["files"])
    finally:
        if BUFFERS.get(session) is buffer:  # cancelled while waiting: the next message starts a buffer of its own
            del BUFFERS[session]


async def tell(cabinet, recipient_id, text):
    """Send ``text`` to the person, in the cabinet's call slots; a refusal by Sparrow (the 24 h window closed) or its
    absence is only logged."""
    try:
        await limited(cabinet, sparrow.send, recipient_id, text)
    except sparrow.SparrowError as error:
        log(f"whatsapp: {cabinet}: not told ({error})")


async def prepared(cabinet, session, email, text, files):
    """Return (reply, files, skipped): a reply when nothing can be run (a limit exceeded, nothing usable in a zip), else
    None with the files Tarik takes (a zip replaced by the files in it) and what it left out, to be run.

    The collaborator is kept as the conversation's sender (people.sender_of) and the files saved before the run."""
    exceeded = problems(text, files)
    if exceeded:
        return "Rien n'a été traité :\n" + "\n".join(f"- {line}" for line in exceeded), files, []
    found, skipped = unpacked([(f["file_name"], f["mime_type"], f["data"]) for f in files], FILE_TYPES)
    files = [{"file_name": name, "mime_type": mime, "data": data} for name, mime, data in found]
    if not text and not files:
        return ("Aucun fichier utilisable dans l'archive : " + " ; ".join(skipped)) if skipped else UNREADABLE, [], []
    await limited(cabinet, remember_whatsapp_sender, session, email)
    if files:
        saved = [{**f, "data": base64.b64encode(f["data"]).decode(), "id": digest(f)} for f in files]
        await call(cabinet, "save_files", session_id=session, files=saved)
    return None, files, skipped


async def run_of(cabinet, session, email, text, files, skipped):
    """Run the Discussion graph on the message in the person's conversation and return what to tell them.

    The run carries the collaborator as its writer, never a Tarik admin. A run stopped by hand (runs.stop) is told as
    such; one that failed is logged, its reply being ERROR."""
    given = {
        "cabinet": cabinet,
        "session_id": session,
        "message": text,
        "files": [f["file_name"] for f in files],
        "skipped": skipped,
    }
    writer = runs.WRITER.set({"email": email, "admin": False})
    try:  # the run started here, and those nested in it, carry it
        run_id, task = await runs.start("discussion", {"input": given, "cabinet": cabinet, "session_id": session})
    finally:
        runs.WRITER.reset(writer)
    try:
        output, failure = await task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():  # this answer itself is cancelled (the server stops): not ours to tell
            raise
        return STOPPED
    if failure:
        log(f"whatsapp: {cabinet} {session}: run {run_id}: {type(failure).__name__}: {failure}")
    return reply_of(output)


async def answer(cabinet, email, session, recipient_id, text, files):
    """Answer the message: check the person still is a collaborator, prepare the message, run it and send the reply by
    WhatsApp. Called with the conversation's turn held (LOCKS).

    Nothing is sent to someone who no longer is a collaborator (as email's refused sender, only logged). A failure
    tells the person once to send again, unless part of a reply already reached them."""
    try:
        if not await limited(cabinet, collaborator, email):
            log(f"whatsapp: {email} is no collaborator of {cabinet}: not answered")
            return
        reply, files, skipped = await prepared(cabinet, session, email, text, files)
        if reply is None:
            reply = await run_of(cabinet, session, email, text, files, skipped)
        await limited(cabinet, sparrow.send, recipient_id, reply)
    except sparrow.SparrowError as error:
        log(f"whatsapp: {cabinet} {session}: the reply: {error}")
        if not error.sent:  # nothing reached them: a refusal of the 24 h window too, told if it can be
            await tell(cabinet, recipient_id, BUSY)
    except Exception as error:
        log(f"whatsapp: {cabinet} {session}: {type(error).__name__}: {error}\n{traceback.format_exc()}")
        await tell(cabinet, recipient_id, BUSY)


async def handler(request: Request):
    """POST /sparrow/handler: a WhatsApp message from Sparrow {message_id, reply_to_message_id, content, and for a file
    mimetype, filename, file (base64)}, with the recipient's recipient_id as bearer.

    401 for a recipient_id nobody is enrolled under, before any read of the body. Otherwise 200 at once ("accepted"),
    also for a message already taken, too heavy or unreadable (the person is told, Sparrow sending it no more): the
    message is answered in the background (answer)."""
    recipient_id = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    who = await recipient_of(recipient_id) if sparrow.RECIPIENT_ID.fullmatch(recipient_id) else None
    if who is None or who[0] not in CABINETS:
        return JSONResponse({"error": "unknown recipient"}, status_code=401)
    cabinet, email, session = who
    body = await body_of(request)
    if body is None:
        launch(tell(cabinet, recipient_id, TOO_LARGE))
        return JSONResponse({"status": "accepted"})
    try:
        payload = json.loads(body)
        message_id = payload["message_id"]
        if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_ID:
            raise ValueError
        text = str(payload.get("content") or "").strip()
        files = []
        if payload.get("file"):
            data = base64.b64decode(payload["file"], validate=True)
            mime = str(payload.get("mimetype") or "")
            files.append(
                {"file_name": named(str(payload.get("filename") or ""), mime), "mime_type": mime, "data": data}
            )
    except ValueError, KeyError, TypeError, AttributeError:
        launch(tell(cabinet, recipient_id, UNREADABLE))
        return JSONResponse({"status": "accepted"})
    if await first_time(message_id):
        gather(cabinet, email, session, recipient_id, text, files)
    return JSONResponse({"status": "accepted"})
