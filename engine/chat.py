"""The Discussion page's conversations with Tarik (server/api/chat.py): each message a run of the "Discussion"
workflow, the cabinet's agent answering in the conversation's own thread. Its files are saved in the cabinet's
schema before the run (save_files), as the mail watcher saves an email's: the run's input names them, never holds
them. As in n8n's chat, whoever writes is the team (logged in to these pages): no sender to check. Also the agent's
asking for the email threads (engine/emails.py)."""

import time
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from engine.calls import within
from engine.tools.core.conversation import for_client, handed, remember_client_message
from engine.tools.emails.inbox import untaken
from engine.tools.invoices.pipeline import waiting_on

# Files no tool took after the reply mean the files tool did not run (engine.tools.emails.inbox.untaken): the agent is
# asked again with a reminder, ASK_TRIES asks at most, none after QUICK seconds; the last reply is given. So is a reply
# that called no tool while a question waits on the client (once: the client may only be thanking): a model replied
# « l'outil n'est pas disponible » to a plain « sans vérification » (2026-10-26).
ASK_TRIES, QUICK = 3, 200
REMINDER = "\n\nRappel : ces fichiers n'ont pas ete traites. Traitez-les maintenant si rien ne manque."
ANSWER_REMINDER = (
    "\n\nRappel : une question attend la réponse du client. Si son message y répond, transmets-la avec "
    "Experio_repondre (l'outil est disponible) ; sinon réponds-lui simplement."
)


def called_a_tool(messages):
    """True when the agent called a tool after the last message of the client, in this turn."""
    for message in reversed(messages):
        if message.type == "human":
            return False
        if message.type == "tool":
            return True
    return False


async def answered(cabinet, session, message, files, node, skipped=()):
    """Tarik's reply to the client's ``message`` and its saved ``files`` (their names), in the conversation
    ``session`` (its own thread): asked again with a reminder while the files stay untaken. Each ask is a run of the
    agent's own, started from the step ``node``."""
    from engine import runs  # runs imports the graphs, this one among them

    text = handed(message, files, skipped)
    started = time.monotonic()
    await within(cabinet, remember_client_message, session, message)
    reminder, answer_reminded = "", False
    for _ in range(ASK_TRIES):
        asked = {"messages": [HumanMessage(text + reminder)]}
        result = await runs.nested("agent", asked, cabinet, session, thread=session, node=node)
        reply = for_client(result["messages"][-1].text)
        if time.monotonic() - started > QUICK:
            return reply
        if files and await within(cabinet, untaken, session):
            reminder = REMINDER
        elif (
            not answer_reminded and not called_a_tool(result["messages"]) and await within(cabinet, waiting_on, session)
        ):
            reminder, answer_reminded = ANSWER_REMINDER, True
        else:
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
    skipped: list  # what a zip of the message held that Tarik does not take, and why (engine.tools.core.archives)


async def ask(state: Chat):
    try:
        given = state["cabinet"], state["session_id"], state["message"], state["files"]
        reply = await answered(*given, "tarik", state.get("skipped", ()))
        return {"reply": reply}
    except Exception:  # no reply (the model failing): told plainly, the message can be sent again
        return {"error": "Tarik n'a pas pu repondre pour le moment. Renvoyez votre message dans un instant."}


chat = StateGraph(Chat, output_schema=Steps)
chat.add_node("tarik", ask)
chat.add_edge(START, "tarik")
chat.add_edge("tarik", END)
graph = chat.compile(name="Tarik - Discussion")
