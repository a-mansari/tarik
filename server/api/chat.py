"""Serve the Discussion page and the dashboard: the team writes to a cabinet's Tarik as the cabinet's clients would,
with files (engine/chat.py).

A conversation's messages are its agent thread's, read from the agent's memory; each message sent starts a Discussion
run, followed on its own page."""

import base64
import hashlib
import re
import time
import uuid
from urllib.parse import quote

from psycopg import sql
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from engine import agent, live, prompts, runs
from engine.calls import call
from engine.chat import REMINDER
from engine.tools.core import archives
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import CABINETS
from engine.tools.core.config import FILE_TYPES, MAX_PAGES
from engine.tools.core.conversation import SKIPPED_NOTE, files_of, own_words
from engine.tools.core.queries import Q
from engine.tools.dossiers.people import ROUTINE_SESSION
from server.api.runs import chosen, cost_shown, markdown, openings, page, spending, usage_shown

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
    """Return ``size`` (bytes) as text in MB, French style ("1,5 Mo")."""
    return f"{size / MB:.1f} Mo".replace(".", ",")


def problems(text, files):
    """Return each limit ``text`` and ``files`` exceed, in plain French: which one, by how much, and how to stay under
    it.

    ChatForm.vue checks the same before sending; this server check is the one that holds. [] when nothing exceeds."""
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
    """True when ``cabinet`` is served and, when ``session`` is given, its id fits the page's own conversations
    (``written``) or any readable conversation."""
    return cabinet in CABINETS and (session is None or (SESSION if written else SHOWN).fullmatch(session))


MEDIUMS = {"discussion": "chat", "fil": "email"}  # the run that answers each message: how the client wrote


def state(status, waiting):
    """Return where a conversation stands (a key of labels.json's conversation_states): "answering", "failed", "waiting"
    or "answered".

    ``status`` is its latest run's; ``waiting`` is True when Tarik waits for the client (a question asked, or drafts
    to post once told to)."""
    if status in ("queued", "running"):
        return "answering"
    if status == "error":
        return "failed"
    return "waiting" if waiting else "answered"


async def waiting_for_client(cabinet, sessions):
    """Return the set of ``sessions`` whose client Tarik waits for (a pending question, or held drafts to post once told
    to)."""
    tables = {key: sql.Identifier(cabinet, f"tarik_{key}") for key in ("pending", "held")}
    query = sql.SQL(Q.sessions_waiting_for_client).format(**tables)
    async with runs.pool.connection() as db:
        rows = await (await db.execute(query, (sessions, sessions))).fetchall()
    return {row[0] for row in rows}


async def conversations(cabinet):
    """Return the cabinet's conversations (Discussion page and email), newest first.

    Each is its latest run {session_id, started, status, graph, ...} plus medium ("chat" or "email"), state (see
    state), and first, subject and sender (see openings)."""
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
    """Return a user-side message as written: {who, text, files, skipped}, who "Client", or "Routine" for what a
    routine sent (agent.ROUTINE_NAME: its instruction, the line its report opens on); None for a reminder."""
    text = agent.text_of(message.content)
    if text.endswith(REMINDER):
        return None
    left = text.partition(SKIPPED_NOTE)[2]
    return {
        "who": "Routine" if message.name == agent.ROUTINE_NAME else "Client",
        "text": own_words(text),
        "files": files_of(text),
        "skipped": left.split(" ; ") if left else [],
    }


async def messages(session):
    """Return the conversation as the client reads it, from the agent's memory.

    Client messages, and each of Tarik's turns with the tools it called (state "running", "done" or "failed") before
    its reply."""
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
            if message.text.strip():  # its last words in the turn: the reply (a routine's report: its tags off)
                turn["text"] = runs.UNTRUSTED_TAGS.sub("", message.text).strip()
        elif message.type == "tool" and message.tool_call_id in called:
            called[message.tool_call_id]["state"] = "failed" if message.status == "error" else "done"
    return shown


async def on_its_way(latest, shown):
    """Return the message of the run still going (``latest``), as sent, while Tarik's memory does not have it yet: a
    client's, or a routine's instruction in a routine's conversation (where the Discussion page never writes: only a
    routine sends there).

    None when it is already the last user-side message in ``shown``."""
    given = (await runs.run(latest["run_id"]))["input"] or {}
    said_before = [entry for entry in shown if entry["who"] in ("Client", "Routine")]
    if said_before and said_before[-1]["text"].strip() == (given.get("message") or "").strip():
        return None
    return {
        "who": "Routine" if latest["session_id"].startswith(ROUTINE_SESSION) else "Client",
        "text": given.get("message", ""),
        "files": given.get("files", []),
        "skipped": given.get("skipped", []),
    }


async def talk(cabinet, session):
    """Return what the conversation shows: {messages, latest, going, made}.

    messages includes the message on its way (its Discussion run still going), so the client's words appear at once;
    latest is the conversation's latest run; going is True while it runs; made lists the files Tarik made for it."""
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
    """GET /ui/api/discussion: return the conversations of the cabinet chosen in the sidebar, each with its model
    calls' tokens and cost (``usage``, usage_shown, None before any), and a new conversation id to start one."""
    cabinet = chosen(request)
    found = await conversations(cabinet) if cabinet else []
    spent = await spending(cabinet, Q.usage_of_sessions, [item["session_id"] for item in found])
    return page(
        {
            "cabinet": cabinet,
            "conversations": [
                {**item, "started": runs.when(item["started"]), "usage": spent.get(item["session_id"])}
                for item in found
            ],
            "new": f"chat-{uuid.uuid4().hex}",
        }
    )


def rendered(shown):
    """Return the messages with their text rendered as HTML under text_html (markdown)."""
    return [{**entry, "text_html": str(markdown(entry["text"])) if entry["text"] else ""} for entry in shown]


async def conversation_page(request: Request):
    """GET /ui/api/discussion/{session_id}: return one conversation of the cabinet chosen in the sidebar: its messages,
    the message on its way, its model calls' tokens and cost (``usage``, usage_shown, None before any), and the
    form's limits.

    The page reads it again at each change of the conversation while Tarik works, so its tools show as they run. 404
    for an unknown conversation."""
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
            "usage": (await spending(cabinet, Q.usage_of_sessions, [session])).get(session),
            **FORM,
        }
    )


async def download(request: Request):
    """GET /ui/api/discussion/{session_id}/fichier?nom=: return a file the client sent in the conversation, as sent,
    else one Tarik made for it.

    Always sent as a download, never opened in the page. 404 when not found."""
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
    """Return a file's id: the first 32 hex characters of its data's SHA-256, as the agent's attachments use
    (engine.agent.attachments), so it is saved once."""
    return hashlib.sha256(file["data"]).hexdigest()[:32]


def refused(why):
    """Return a 400 response {error: ``why``}."""
    return JSONResponse({"error": why}, status_code=400)


async def send(request: Request):
    """POST /ui/api/discussion/{session_id}: send a message {cabinet, message, files: [{file_name, mime_type, data
    (base64)}]} and start a Discussion run.

    The files are checked (see problems), zips unpacked, and the files saved in the cabinet's schema for this
    conversation before the run starts. Returns {run_id, skipped} at once (202). Refused (400) when the sidebar has
    chosen another cabinet since the page showed ``cabinet``, so a message never goes to the wrong cabinet."""
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
    """Return the cabinet's last DAYS days in four numbers, {runs, seconds, failed, conversations}, and its model
    calls per model, ``usage``: [(model, input, cached, output, cost, priced)] (dashboard_usage).

    Read once per change of the cabinet (engine/live.py), or after KEPT_SECONDS, and shared by every dashboard open
    on it."""
    version = live.versions([("cabinet", cabinet)])
    if cabinet in NUMBERS:
        read_version, read_at, kept = NUMBERS[cabinet]
        if read_version == version and time.monotonic() - read_at < KEPT_SECONDS:
            return kept
    async with runs.pool.connection() as db:
        count, seconds, failed, talks = await (await db.execute(Q.dashboard_metrics, (cabinet, DAYS))).fetchone()
        usage = await (await db.execute(prompts.of(Q.dashboard_usage, cabinet), (DAYS,))).fetchall()
    metrics = {"runs": count, "seconds": seconds, "failed": failed, "conversations": talks, "usage": usage}
    NUMBERS[cabinet] = (version, time.monotonic(), metrics)
    return metrics


def readable(metrics):
    """Return the four numbers as the team reads them: mean duration in s or min s, failed share in %, "—" when none;
    and the model calls' cost (the priced ones') with each model's tokens and cost.

    ``bad`` is True when some runs failed."""
    seconds, failed = metrics["seconds"], metrics["failed"]
    if seconds is None:
        duration = "—"
    elif seconds < 60:
        duration = f"{seconds:.1f} s".replace(".", ",")
    else:
        duration = f"{int(seconds // 60)} min {round(seconds % 60)} s"
    share = "—" if failed is None else f"{round(failed * 100, 1):g} %".replace(".", ",")
    usage = metrics["usage"]
    known = [row[4] for row in usage if row[4] is not None]
    return {
        "cost": cost_shown(sum(known) if known else None, all(row[5] for row in usage)) if usage else "—",
        "models": [{"model": model, **usage_shown(*row)} for model, *row in usage],
        "runs": str(metrics["runs"]),
        "seconds": duration,
        "failed": share,
        "conversations": str(metrics["conversations"]),
        "bad": bool(failed),
    }


async def dashboard_page(request: Request):
    """GET /ui/api/accueil: return the dashboard of the cabinet chosen in the sidebar: its last DAYS days in four
    numbers and its model calls' cost, a new conversation id (``new``) and the form's limits.

    The page reads it again at each change of the cabinet."""
    cabinet = chosen(request)
    metrics = readable(await metrics_of(cabinet)) if cabinet else None
    return page({"cabinet": cabinet, "metrics": metrics, "days": DAYS, "new": f"chat-{uuid.uuid4().hex}", **FORM})
