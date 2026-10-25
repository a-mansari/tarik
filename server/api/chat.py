"""The Discussion page: the team writes to a cabinet's Tarik, as the cabinet's clients would, with files
(engine/chat.py). A conversation's messages are its agent thread's, read from the agent's memory; each message sent
is a Discussion run, followed on its own page."""

import base64
import hashlib
import re
import time
import uuid
from urllib.parse import quote

from psycopg import sql
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from engine import agent, live, runs
from engine.calls import call
from engine.chat import REMINDER
from engine.tools.core import archives
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import CABINETS
from engine.tools.core.config import FILE_TYPES, MAX_PAGES
from engine.tools.core.conversation import SKIPPED_NOTE, files_of, own_words
from engine.tools.core.queries import Q
from server.api.runs import chosen, markdown, openings, page

SESSION = re.compile(r"chat-[0-9a-f]{32}")  # the page's own conversations: the only ones written to from here
SHOWN = re.compile(r"[\w.@-]{1,120}")  # any conversation's id, to read it (an email thread's, a workflow's)
MB = 1024 * 1024
MAX_FILES, MAX_FILE, MAX_TEXT = 10, 20 * MB, 4000  # files per message, bytes per file, characters
ACCEPTED = (*FILE_TYPES, ".zip")  # a zip is opened before anything is saved (engine.tools.core.archives)
TYPES = ", ".join(kind.lstrip(".").upper() for kind in ACCEPTED)
# Every cap on what a message carries, as the chat page lists them; each refusal names the one exceeded (problems).
LIMITS = [
    ("Types acceptés", f"{TYPES} (factures et relevés en PDF ou photo ; FEC et grand livre en CSV, TXT ou Excel)"),
    ("Par message", f"{MAX_FILES} fichiers au plus ; {MAX_TEXT} caractères au plus"),
    ("Par fichier", f"{MAX_FILE // MB} Mo au plus, non vide"),
    (
        "Archive ZIP",
        f"{archives.MAX_FILES} fichiers et {archives.MAX_TOTAL // MB} Mo décompressés au plus, "
        f"{archives.MAX_FILE // MB} Mo par fichier décompressé ; une seule archive dans l'archive ; sans mot de "
        "passe. Ce qui est laissé de côté vous est dit.",
    ),
    ("Photos", "toute taille : redressées et réduites pour la lecture"),
    ("Lecture", f"les {MAX_PAGES} premières pages d'une facture donnent ses informations"),
]
# What the page needs to show the limits and check them before sending (server/web/src/components/ChatForm.vue).
FORM = {
    "accepted": ",".join(ACCEPTED),
    "limits": LIMITS,
    "checks": {
        "accepted": ACCEPTED,
        "max_files": MAX_FILES,
        "max_file": MAX_FILE,
        "max_text": MAX_TEXT,
        "types": TYPES,
    },
}


def megabytes(size):
    return f"{size / MB:.1f} Mo".replace(".", ",")


def problems(text, files):
    """Each cap ``text`` and ``files`` exceed, in plain French: which one, by how much, and how to stay under it.
    ChatForm.vue says the same before sending; this is what holds."""
    found = []
    if len(text) > MAX_TEXT:
        found.append(f"Message de {len(text)} caractères : {MAX_TEXT} au plus. Raccourcissez-le.")
    if len(files) > MAX_FILES:
        found.append(
            f"{len(files)} fichiers joints : {MAX_FILES} au plus par message. Envoyez les autres dans un second "
            "message, ou réunissez-les dans un zip."
        )
    for file in files:
        name, size = file["file_name"], len(file["data"])
        if not name.lower().endswith(ACCEPTED) or "/" in name:
            found.append(f"{name} : type non accepté. Types acceptés : {TYPES}.")
        elif not size:
            found.append(f"{name} : fichier vide.")
        elif size > MAX_FILE:
            found.append(
                f"{name} : {megabytes(size)}, {MAX_FILE // MB} Mo au plus par fichier. Envoyez-le compressé ou en "
                "plusieurs parties."
            )
    return found


def known(cabinet, session=None, written=True):
    """A cabinet served and, when given, a conversation of it: one of the page's own (``written``), else any to read."""
    return cabinet in CABINETS and (session is None or (SESSION if written else SHOWN).fullmatch(session))


MEDIUMS = {"discussion": "chat", "fil": "email"}  # the run that answers each message: how the client wrote


def state(status, waiting):
    """Where a conversation stands, as the team reads it: Tarik answering, failed, waiting for its client (a question
    asked, drafts to post once told to), or answered. A key of labels.json's conversation_states."""
    if status in ("queued", "running"):
        return "answering"
    if status == "error":
        return "failed"
    return "waiting" if waiting else "answered"


async def waiting_for_client(cabinet, sessions):
    """Those of ``sessions`` whose client Tarik waits for (a question asked, drafts to post once told to): a set."""
    tables = {key: sql.Identifier(cabinet, f"tarik_{key}") for key in ("pending", "held")}
    query = sql.SQL(Q.sessions_waiting_for_client).format(**tables)
    async with runs.pool.connection() as db:
        rows = await (await db.execute(query, (sessions, sessions))).fetchall()
    return {row[0] for row in rows}


async def conversations(cabinet):
    """The cabinet's conversations, from the Discussions page and by email, latest first: their latest message's run
    {session_id, started, status, graph...}, how the client writes (``medium``: chat or email), the first message's
    words (``first``, cut, else its files) and for an email its subject and sender."""
    latest = []
    for graph in MEDIUMS:
        latest += await runs.recent(cabinet, graph, 200)
    found = {}
    for run in sorted(latest, key=lambda run: run["started"], reverse=True):
        found.setdefault(run["session_id"], {**run, "medium": MEDIUMS[run["graph"]]})
    waiting = await waiting_for_client(cabinet, list(found))
    for session, run in found.items():
        run["state"] = state(run["status"], session in waiting)
    for session, opening in (await openings(cabinet, list(found))).items():
        found[session].update(opening)
    return list(found.values())


def said(message):
    """A client's message as written: its text and the names of its files; None for a reminder Tarik was sent."""
    text = agent.text_of(message.content)
    if text.endswith(REMINDER):
        return None
    left = text.partition(SKIPPED_NOTE)[2]
    return {
        "who": "Client",
        "text": own_words(text),
        "files": files_of(text),
        "skipped": left.split(" ; ") if left else [],
    }


async def messages(session):
    """The conversation as the client reads it, as n8n's chat shows one: their messages, and each of Tarik's turns
    with the tools it called (done, failed, or still going) before its reply."""
    saved = await agent.CHECKPOINTER.aget({"configurable": {"thread_id": session, "checkpoint_ns": ""}})
    shown, called = [], {}  # tool call id -> its line in the turn
    for message in (saved or {}).get("channel_values", {}).get("messages", []):
        if message.type == "human":
            entry = said(message)
            if entry:
                shown.append(entry)
        elif message.type == "ai":
            if not shown or shown[-1]["who"] != "Tarik":
                shown.append({"who": "Tarik", "tools": [], "text": ""})
            turn = shown[-1]
            for tool_call in message.tool_calls:
                called[tool_call["id"]] = {"name": tool_call["name"], "state": "running"}
                turn["tools"].append(called[tool_call["id"]])
            if message.text.strip():
                turn["text"] = message.text  # its last words in the turn: the reply
        elif message.type == "tool" and message.tool_call_id in called:
            called[message.tool_call_id]["state"] = "failed" if message.status == "error" else "done"
    return shown


async def on_its_way(latest, shown):
    """The client's message of the run ``latest`` still going, as sent, while Tarik's memory does not have it yet
    (not the last of the client's messages ``shown``); else None."""
    given = (await runs.run(latest["run_id"]))["input"] or {}
    clients = [entry for entry in shown if entry["who"] == "Client"]
    if clients and clients[-1]["text"].strip() == (given.get("message") or "").strip():
        return None
    return {
        "who": "Client",
        "text": given.get("message", ""),
        "files": given.get("files", []),
        "skipped": given.get("skipped", []),
    }


async def talk(cabinet, session):
    """What the conversation shows: its messages, and the message on its way (its Discussion run still going), its
    client's words shown at once, before Tarik's memory has them; the files Tarik made for it (``made``)."""
    shown = await messages(session)
    latest = next((run for run in await conversations(cabinet) if run["session_id"] == session), None)
    going = bool(latest and latest["status"] in ("queued", "running"))
    if going and latest["medium"] == "chat":  # an email thread's message is in Tarik's memory once its run asks
        pending = await on_its_way(latest, shown)
        if pending:
            shown.append(pending)
    outbox = sql.Identifier(cabinet, "tarik_outbox")
    async with runs.pool.connection() as db:
        made = await (await db.execute(sql.SQL(Q.chat_outbox_names).format(outbox=outbox), (session,))).fetchall()
    return {"messages": shown, "latest": latest, "going": going, "made": [name for (name,) in made]}


async def chat_page(request: Request):
    """The conversations of the cabinet picked in the sidebar, and a new one to start."""
    cabinet = chosen(request)
    found = await conversations(cabinet) if cabinet else []
    return page(
        {
            "cabinet": cabinet,
            "conversations": [{**item, "started": runs.when(item["started"])} for item in found],
            "new": f"chat-{uuid.uuid4().hex}",
        }
    )


def rendered(shown):
    """The messages with their words as the page shows them: markdown rendered here, its HTML never kept."""
    return [{**entry, "text_html": str(markdown(entry["text"])) if entry["text"] else ""} for entry in shown]


async def conversation_page(request: Request):
    """One conversation: its messages, the message on its way, and the form's limits; the page reads it again at
    each change of the conversation while Tarik works (its tools showing as they run). The cabinet picked in the
    sidebar's."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    if not known(cabinet, session, written=False):
        return page({"error": "Discussion introuvable."}, 404)
    talked = await talk(cabinet, session)
    return page(
        {
            "cabinet": cabinet,
            "session_id": session,
            "written": bool(SESSION.fullmatch(session)),
            **talked,
            "messages": rendered(talked["messages"]),
            **FORM,
        }
    )


async def download(request: Request):
    """A file the client sent in the conversation (``?nom=``), as sent, else one Tarik made for it: always downloaded,
    never opened in the page."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    name = request.query_params.get("nom", "")
    if not known(cabinet, session, written=False):
        return page({"error": "Discussion introuvable."}, 404)
    files = sql.Identifier(cabinet, "tarik_files")
    async with runs.pool.connection() as db:
        found = await (await db.execute(sql.SQL(Q.chat_file).format(files=files), (session, name))).fetchone()
        if not found:  # a file Tarik made for it (an export)
            outbox = sql.Identifier(cabinet, "tarik_outbox")
            found = await (
                await db.execute(sql.SQL(Q.chat_outbox_file).format(outbox=outbox), (session, name))
            ).fetchone()
    if not found:
        return page({"error": "Fichier introuvable."}, 404)
    file_name, mime_type, data = found
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(bytes(data), media_type=mime_type or "application/octet-stream", headers=headers)


def digest(file):
    """A file's id, its data's hash, as the agent's attachments have (engine.agent.attachments): saved once."""
    return hashlib.sha256(file["data"]).hexdigest()[:32]


def refused(why):
    return JSONResponse({"error": why}, status_code=400)


async def send(request: Request):
    """A message sent: {message, files: [{file_name, mime_type, data (base64)}]}. Its files saved in the cabinet's
    schema for this conversation, then a Discussion run started; its run_id at once. ``cabinet``, the one the page
    showed: refused when the sidebar picked another since, never sent to the wrong cabinet."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    if not known(cabinet, session):
        return refused("Discussion inconnue.")
    if int(request.headers.get("content-length") or 0) > MAX_FILES * MAX_FILE * 4 // 3 + 64 * 1024:
        return refused(f"Message trop lourd : {MAX_FILES} fichiers de {MAX_FILE // MB} Mo au plus par message.")
    try:
        payload = await request.json()
        text = str(payload.get("message") or "").strip()
        files = [
            {
                "file_name": str(file["file_name"]).strip(),
                "mime_type": str(file.get("mime_type") or "application/octet-stream"),
                "data": base64.b64decode(file["data"], validate=True),
            }
            for file in payload.get("files") or []
        ]
    except ValueError, KeyError, TypeError:
        return refused("Message illisible.")
    if payload.get("cabinet") != cabinet:
        return refused("Un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la.")
    if not text and not files:
        return refused("Écrivez un message ou joignez un fichier.")
    exceeded = problems(text, files)
    if exceeded:
        return refused("Rien n'a été envoyé :\n" + "\n".join(f"- {line}" for line in exceeded))
    # A zip, replaced by the files in it Tarik takes; what it leaves out is told to the person who sent it.
    found, skipped = unpacked([(f["file_name"], f["mime_type"], f["data"]) for f in files], FILE_TYPES)
    files = [{"file_name": name, "mime_type": mime, "data": data} for name, mime, data in found]
    if not text and not files:
        return refused("Aucun fichier utilisable dans l'archive : " + " ; ".join(skipped))
    if files:
        await call(
            cabinet,
            "save_files",
            session_id=session,
            files=[{**file, "data": base64.b64encode(file["data"]).decode(), "id": digest(file)} for file in files],
        )
    given = {
        "cabinet": cabinet,
        "session_id": session,
        "message": text,
        "files": [f["file_name"] for f in files],
        "skipped": skipped,
    }
    run_id, _ = await runs.start("discussion", {"input": given, "cabinet": cabinet, "session_id": session})
    return JSONResponse({"run_id": run_id, "skipped": skipped}, status_code=202)


DAYS = 30  # the dashboard's period
KEPT_SECONDS = 300  # a cabinet's numbers, read again at its next change, or after this (the period moves on)
NUMBERS = {}  # cabinet: (its changes' count when read, when, its numbers)


async def metrics_of(cabinet):
    """The cabinet's last DAYS days in four numbers: read once for each change of the cabinet (engine/live.py), and
    kept for every dashboard open on it, however many follow it."""
    version = live.versions([("cabinet", cabinet)])
    if cabinet in NUMBERS:
        read_version, read_at, kept = NUMBERS[cabinet]
        if read_version == version and time.monotonic() - read_at < KEPT_SECONDS:
            return kept
    async with runs.pool.connection() as db:
        count, seconds, failed, talks = await (await db.execute(Q.dashboard_metrics, (cabinet, DAYS))).fetchone()
    metrics = {"runs": count, "seconds": seconds, "failed": failed, "conversations": talks}
    NUMBERS[cabinet] = (version, time.monotonic(), metrics)
    return metrics


def readable(metrics):
    """The four numbers as the team reads them: a mean duration in s or min s, a share failed in %, "—" for none;
    ``bad`` when some failed."""
    seconds, failed = metrics["seconds"], metrics["failed"]
    if seconds is None:
        duration = "—"
    elif seconds < 60:
        duration = f"{seconds:.1f} s".replace(".", ",")
    else:
        duration = f"{int(seconds // 60)} min {round(seconds % 60)} s"
    share = "—" if failed is None else f"{round(failed * 100, 1):g} %".replace(".", ",")
    return {
        "runs": str(metrics["runs"]),
        "seconds": duration,
        "failed": share,
        "conversations": str(metrics["conversations"]),
        "bad": bool(failed),
    }


async def dashboard_page(request: Request):
    """The cabinet picked: its last DAYS days in four numbers, read again by the page at each change of the cabinet,
    and a message to Tarik that opens a new conversation (``new``) with the form's limits."""
    cabinet = chosen(request)
    metrics = readable(await metrics_of(cabinet)) if cabinet else None
    return page({"cabinet": cabinet, "metrics": metrics, "days": DAYS, "new": f"chat-{uuid.uuid4().hex}", **FORM})
