"""The Discussion page: the team writes to a cabinet's Tarik, as the cabinet's clients would, with files
(engine/chat.py). A conversation's messages are its agent thread's, read from the agent's memory; each message sent
is a Discussion run, followed on its own page."""

import base64
import hashlib
import re
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse

from engine import agent, runs
from engine.chat import REMINDER
from engine.tools import call
from tarik.core.cabinets import CABINETS
from tarik.core.conversation import FILES_NOTE, NO_TEXT
from tarik.emails.inbox import FILE_TYPES
from ui.pages import templates

SESSION = re.compile(r"chat-[0-9a-f]{32}")  # the page's own conversations only: never an email thread's
MAX_FILES, MAX_FILE, MAX_TEXT = 10, 20 * 1024 * 1024, 4000  # files per message, bytes per file, characters


def known(cabinet, session=None):
    return cabinet in CABINETS and (session is None or SESSION.fullmatch(session))


async def conversations(cabinet):
    """The cabinet's Discussion conversations, latest first: {session_id, last, status} of each one's latest message."""
    found = {}
    for run in await runs.recent(cabinet, "discussion", 200):
        found.setdefault(run["session_id"], run)
    return list(found.values())


def said(message):
    """A client's message as written: its text and the names of its files; None for a reminder Tarik was sent."""
    text = agent.text_of(message.content)
    if text.endswith(REMINDER):
        return None
    words, _, files = text.partition(FILES_NOTE)
    shown = "" if words.strip() == NO_TEXT else words
    return {"who": "Client", "text": shown, "files": files.split(", ") if files else []}


async def messages(session):
    """The conversation as the client reads it: their messages and Tarik's replies, none of its tools' calls."""
    saved = await agent.CHECKPOINTER.aget({"configurable": {"thread_id": session, "checkpoint_ns": ""}})
    shown = []
    for message in (saved or {}).get("channel_values", {}).get("messages", []):
        if message.type == "human" and (entry := said(message)):
            shown.append(entry)
        elif message.type == "ai" and not message.tool_calls and message.text.strip():
            shown.append({"who": "Tarik", "text": message.text})
    return shown


async def chat_page(request: Request):
    """A cabinet's conversations, and a new one to start."""
    cabinets = sorted(CABINETS)
    cabinet = request.query_params.get("cabinet")
    cabinet = cabinet if cabinet in CABINETS else (cabinets[0] if cabinets else None)
    context = {
        "cabinets": cabinets,
        "cabinet": cabinet,
        "conversations": await conversations(cabinet) if cabinet else [],
        "new": f"chat-{uuid.uuid4().hex}",
    }
    return templates.TemplateResponse(request, "chat.html", context)


async def conversation_page(request: Request):
    """One conversation: its messages, the message on its way (a Discussion run still going), and the form."""
    cabinet, session = request.path_params["cabinet"], request.path_params["session_id"]
    if not known(cabinet, session):
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    latest = next(iter([r for r in await conversations(cabinet) if r["session_id"] == session]), None)
    context = {
        "cabinet": cabinet,
        "session_id": session,
        "messages": await messages(session),
        "latest": latest,
        "accepted": ",".join(FILE_TYPES),
        "max_files": MAX_FILES,
        "max_mb": MAX_FILE // (1024 * 1024),
    }
    return templates.TemplateResponse(request, "chat_conversation.html", context)


def digest(file):
    """A file's id, its data's hash, as the agent's attachments have (engine.agent.attachments): saved once."""
    return hashlib.sha256(file["data"]).hexdigest()[:32]


def refused(why):
    return JSONResponse({"error": why}, status_code=400)


async def send(request: Request):
    """A message sent: {message, files: [{file_name, mime_type, data (base64)}]}. Its files saved in the cabinet's
    schema for this conversation, then a Discussion run started; its run_id at once."""
    cabinet, session = request.path_params["cabinet"], request.path_params["session_id"]
    if not known(cabinet, session):
        return refused("Conversation inconnue.")
    if int(request.headers.get("content-length") or 0) > MAX_FILES * MAX_FILE * 4 // 3 + 64 * 1024:
        return refused("Message trop lourd.")
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
    if not text and not files:
        return refused("Écrivez un message ou joignez un fichier.")
    if len(text) > MAX_TEXT:
        return refused(f"Message trop long : {MAX_TEXT} caractères au plus.")
    if len(files) > MAX_FILES:
        return refused(f"{MAX_FILES} fichiers au plus par message.")
    for file in files:
        if not file["file_name"].lower().endswith(FILE_TYPES) or "/" in file["file_name"]:
            return refused(f"{file['file_name']} : type non accepté ({', '.join(FILE_TYPES)}).")
        if not file["data"] or len(file["data"]) > MAX_FILE:
            return refused(f"{file['file_name']} : vide, ou plus de {MAX_FILE // (1024 * 1024)} Mo.")
    if files:
        await call(
            cabinet,
            "save_files",
            session_id=session,
            files=[{**file, "data": base64.b64encode(file["data"]).decode(), "id": digest(file)} for file in files],
        )
    given = {"cabinet": cabinet, "session_id": session, "message": text, "files": [f["file_name"] for f in files]}
    run_id, _ = await runs.start("discussion", {"input": given, "cabinet": cabinet, "session_id": session})
    return JSONResponse({"run_id": run_id}, status_code=202)
