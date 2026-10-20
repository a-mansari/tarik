"""The Discussion page's conversations with Tarik (ui/chat): each message a run of the "Discussion" workflow, the
cabinet's agent answering in the conversation's own thread. Its files are saved in the cabinet's schema before the
run (save_files), as the mail watcher saves an email's: the run's input names them, never holds them. As in n8n's
chat, whoever writes is the team (logged in to these pages): no sender to check. Also the agent's asking for the
email threads (engine/emails.py)."""

import time
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from engine.tools import within
from tarik.core.conversation import FILES_NOTE, NO_TEXT, remember_client_message
from tarik.emails.inbox import untaken

# Files no tool took after the reply mean the files tool did not run (tarik.emails.inbox.untaken): the agent is
# asked again with a reminder, ASK_TRIES asks at most, none after QUICK seconds; the last reply is given.
ASK_TRIES, QUICK = 3, 200
REMINDER = "\n\nRappel : ces fichiers n'ont pas ete traites. Traitez-les maintenant si rien ne manque."


async def answered(cabinet, session, message, files, node):
    """Tarik's reply to the client's ``message`` and its saved ``files`` (their names), in the conversation
    ``session`` (its own thread): asked again with a reminder while the files stay untaken. Each ask is a run of the
    agent's own, started from the step ``node``."""
    from engine import runs  # runs imports the graphs, this one among them

    text = (message or NO_TEXT) + (FILES_NOTE + ", ".join(files) if files else "")
    started = time.monotonic()
    await within(cabinet, remember_client_message, session, message)
    for attempt in range(ASK_TRIES):
        asked = {"messages": [HumanMessage(text + (REMINDER if attempt else ""))]}
        result = await runs.nested("agent", asked, cabinet, session, thread=session, node=node)
        reply = result["messages"][-1].text
        if not files or time.monotonic() - started > QUICK or not await within(cabinet, untaken, session):
            return reply
    return reply


class Steps(TypedDict, total=False):
    reply: str
    error: str


class Chat(Steps, total=False):
    cabinet: str
    session_id: str
    message: str
    files: list  # the names of the files saved with the message


async def ask(state: Chat):
    try:
        reply = await answered(state["cabinet"], state["session_id"], state["message"], state["files"], "tarik")
        return {"reply": reply}
    except Exception:  # no reply (the model failing): told plainly, the message can be sent again
        return {"error": "Tarik n'a pas pu repondre pour le moment. Renvoyez votre message dans un instant."}


chat = StateGraph(Chat, output_schema=Steps)
chat.add_node("tarik", ask)
chat.add_edge(START, "tarik")
chat.add_edge("tarik", END)
graph = chat.compile(name="Tarik - Discussion")
