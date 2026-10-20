"""ʺTarik - Emailsʺ (workflows/build_tarik_emails.py) as a graph: for the cabinet named, each thread waiting for an
answer is claimed, acknowledged, answered by the cabinet's agent (the agent graph, in the thread's own conversation:
no n8n MCP call_agent in between; reminded of files still untaken, as ask_agent does), and the reply sent to
whitelisted addresses. A sender refused (check_sender) gets nothing: the manager is told, the thread stays set aside. A follow-up thread runs Traitement
then Dossier in mode resume and sends their summaries."""

import operator
import re
import time
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from psycopg import sql

from engine import agent, dossier, traitement
from engine.tools import ToolFailure, call

FILES_NOTE, NO_TEXT, NOTHING = "\n\nFichiers joints : ", "(pas de message)", "Rien n'a ete traite."
# As ask_agent (mcp/tarik/emails/agent.py): files no tool took after the reply mean the files tool did not run; the
# agent is asked again with a reminder, ASK_TRIES asks at most, none after QUICK seconds; the last reply is sent.
ASK_TRIES, QUICK = 3, 200
REMINDER = "\n\nRappel : ces fichiers n'ont pas ete traites. Traitez-les maintenant si rien ne manque."
UNTAKEN = (
    "select count(*) from {} where session_id = %s and parent_id is null and kind is null "
    "and invoice_id is null and statement_id is null"
)


class Run(TypedDict, total=False):
    cabinet: str
    threads: list
    done: Annotated[list, operator.add]


class Thread(TypedDict, total=False):
    cabinet: str
    thread: dict
    reply: str
    error: str
    refusal: dict
    done: Annotated[list, operator.add]


async def claim(state: Run):
    result = await call(state["cabinet"], "claim_threads")
    return {"threads": result.get("threads", [])}


def each_thread(state: Run):
    return [Send("fil", {"cabinet": state["cabinet"], "thread": thread, "done": []}) for thread in state["threads"]] or END


def kind(state: Thread):
    return "suite" if state["thread"]["follow_up"] else "accuse"


async def acknowledge(state: Thread):
    try:  # not sent (whitelist, SMTP): the agent answers all the same
        await call(state["cabinet"], "acknowledge_thread", thread_id=state["thread"]["thread_id"])
    except Exception:
        pass
    return {}


async def remember(cabinet, session_id, message):
    """The client's own words kept for the conversation's later checks (remember_client_message, in the cabinet's
    schema): the email's text, never the files' names ask_agent adds."""
    from engine.runs import pool  # set once the server runs; runs imports this module

    if not (message or "").strip() or message.strip() == NO_TEXT:
        return
    table = sql.Identifier(cabinet, "tarik_client_messages")
    async with pool.connection() as db:
        last = await (await db.execute(sql.SQL("select message from {} where session_id = %s order by id desc limit 1").format(table), (session_id,))).fetchone()
        if not last or last[0] != message:
            await db.execute(sql.SQL("insert into {} (session_id, message) values (%s, %s)").format(table), (session_id, message))


async def untaken(cabinet, session_id):
    from engine.runs import pool

    async with pool.connection() as db:
        return (await (await db.execute(sql.SQL(UNTAKEN).format(sql.Identifier(cabinet, "tarik_files")), (session_id,))).fetchone())[0]


async def ask(state: Thread):
    """The cabinet's agent, in the thread's own conversation (its session id as thread id), asked again with a
    reminder while the thread's files stay untaken."""
    thread, cabinet = state["thread"], state["cabinet"]
    text = (thread["message"] or NO_TEXT) + (FILES_NOTE + ", ".join(thread["files"]) if thread["files"] else "")
    thread_id = thread["session_id"]  # the email thread's own session: its conversation, its files
    config = {"configurable": {"thread_id": thread_id, "session_id": thread["session_id"], "cabinet": cabinet}}
    started = time.monotonic()
    try:
        await remember(cabinet, thread["session_id"], thread["message"])
        for attempt in range(ASK_TRIES):
            result = await agent.make_agent(config).ainvoke({"messages": [HumanMessage(text + (REMINDER if attempt else ""))]}, config=config)
            reply = result["messages"][-1].text
            if not thread["files"] or time.monotonic() - started > QUICK or not await untaken(cabinet, thread["session_id"]):
                break
        return {"reply": reply}
    except Exception:  # no reply (the model failing): set aside, with why, never asked twice
        return {"error": "Tarik n'a pas pu repondre pour le moment."}


async def refusal(state: Thread):
    try:
        return {"refusal": await call(state["cabinet"], "thread_refusal", thread_id=state["thread"]["thread_id"])}
    except Exception as error:  # set aside with why, as n8n does
        return {"error": failure_text(error)}


async def notify(state: Thread):
    found = state["refusal"]
    try:  # no manager set, or the email not sent: nothing more, as in n8n
        await call(state["cabinet"], "notify_manager", sender=found["sender"], subject=found["subject"], reason=found["reason"])
    except Exception:
        pass
    return {}


def failure_text(error):
    """Why a thread is set aside, as n8n words it: the tool's message without the MCP prefix."""
    if not isinstance(error, ToolFailure):
        return f"Erreur ({type(error).__name__})."
    return re.sub(r"^Error executing tool \w+: ", "", str(error))


async def send(state: Thread):
    thread = state["thread"]
    try:
        await call(state["cabinet"], "send_email", to=thread["to"], subject=thread["subject"], body=state["reply"], in_reply_to=thread["in_reply_to"], references=thread["references"])
    except Exception as error:
        return {"error": failure_text(error)}
    return {}


async def follow_up(state: Thread):
    """A follow-up: no agent, no time limit. Traitement then Dossier in mode resume; one email tells both."""
    thread = state["thread"]
    fixed = {"mode": "resume", "session_id": thread["session_id"], "cabinet": state["cabinet"]}
    summaries = []
    try:
        for graph in (traitement.graph, dossier.graph):
            result = await graph.ainvoke({"call": fixed, "outcomes": []})
            if result["message"] != NOTHING:
                summaries.append(result["message"])
    except Exception as error:  # set aside with why, as n8n does
        return {"error": failure_text(error)}
    return {"reply": "Suite du traitement de vos documents :\n\n" + "\n\n".join(summaries)}


async def finish(state: Thread):
    thread = state["thread"]
    sent = not state.get("error")
    await call(state["cabinet"], "finish_thread", thread_id=thread["thread_id"], message_ids=thread["message_ids"], sent=sent, reason=state.get("error", ""))
    return {"done": [{"thread_id": thread["thread_id"], "sent": sent, "reason": state.get("error", "")}]}


fil = StateGraph(Thread, output_schema=Run)
for name, node in {"accuse": acknowledge, "tarik": ask, "refus": refusal, "manager": notify, "reponse": send, "suite": follow_up, "fin": finish}.items():
    fil.add_node(name, node)
fil.add_conditional_edges(START, kind, ["accuse", "suite"])
fil.add_edge("accuse", "tarik")
fil.add_conditional_edges("tarik", lambda s: "fin" if s.get("error") else "refus", ["fin", "refus"])
fil.add_conditional_edges("refus", lambda s: "fin" if s.get("error") else "manager" if s["refusal"].get("refused") else "reponse", ["fin", "manager", "reponse"])
fil.add_edge("manager", END)  # thread_refusal set the thread aside and counted its emails: finish_thread would undo it
fil.add_edge("reponse", "fin")
fil.add_conditional_edges("suite", lambda s: "fin" if s.get("error") else "reponse", ["fin", "reponse"])
fil.add_edge("fin", END)
FIL = fil.compile(name="fil")

run = StateGraph(Run)
run.add_node("fils", claim)
run.add_node("fil", FIL)
run.add_edge(START, "fils")
run.add_conditional_edges("fils", each_thread, ["fil", END])
run.add_edge("fil", END)
graph = run.compile(name="Tarik - Emails")
