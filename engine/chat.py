"""The Discussion page's conversations with Tarik (server/api/chat.py).

Each message is a run of the "Discussion" graph: the cabinet's agent answers in the conversation's own thread. The
message's files are saved in the cabinet's schema before the run (save_files), as the mail watcher saves an
email's; the run's input names them and never holds their data. Whoever writes here is logged in to the team's
pages, so there is no sender to check. answered() is also how the email threads ask the agent (engine/emails.py)."""

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
    """True when ``messages`` hold a tool message after the client's last message (the agent called a tool this
    turn)."""
    for message in reversed(messages):
        if message.type == "human":
            return False
        if message.type == "tool":
            return True
    return False


# Tarik's own earlier email the client answers (a routine's report), handed before their words: what « oui » means.
# Its text holds Experio's data (dossier names): wrapped as untrusted, never read as the client's words.
EARLIER = "Votre email précédent dans ce fil, auquel le client répond :\n{}\n\n"


async def answered(cabinet, session, message, files, node, skipped=(), earlier=""):
    """Ask the agent about the client's ``message`` and return its reply, cleaned for the client (for_client).

    Args:
        cabinet: the cabinet to run in.
        session: the conversation's id, also the agent's thread.
        message: the client's words.
        files: the names of the files saved with the message.
        node: the graph node each agent run is nested under (runs.nested).
        skipped: what a zip held that Tarik does not take, and why; added to the agent's message (handed).
        earlier: Tarik's own email the client answers, handed once before their words (EARLIER, as untrusted
            data), never theirs: the tools read past it (agent.client_message), what they wrote alone confirms or
            names something (remember_client_message).

    The agent is asked again, at most ASK_TRIES times in all and only within QUICK seconds, with REMINDER while
    ``files`` stay untaken, or once with ANSWER_REMINDER when it called no tool while a question waits on the client.
    The last reply is returned."""
    from engine import runs  # runs imports the graphs, this one among them
    from engine.agent import EARLIER_LABEL, untrusted

    text = handed(message, files, skipped)
    first = (EARLIER.format(untrusted(earlier, EARLIER_LABEL)) if earlier else "") + text  # once: then it is history
    started = time.monotonic()
    await within(cabinet, remember_client_message, session, message)
    reminder, answer_reminded = "", False
    for _ in range(ASK_TRIES):
        asked = {"messages": [HumanMessage((text if reminder else first) + reminder)]}
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
    """Graph node: set "reply" to the agent's answer, or "error" to a plain retry message when the agent failed."""
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
