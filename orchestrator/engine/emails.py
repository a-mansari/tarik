"""ʺTarik - Emailsʺ (workflows/build_tarik_emails.py) as a graph: for the cabinet named, each thread waiting for an
answer is claimed, acknowledged, answered by the cabinet's agent (the agent graph, in the thread's own conversation:
no n8n MCP call_agent in between), and the reply sent to whitelisted addresses. A follow-up thread runs Traitement
then Dossier in mode resume and sends their summaries."""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine import agent, dossier, traitement
from engine.tools import ToolFailure, call

FILES_NOTE, NO_TEXT, NOTHING = "\n\nFichiers joints : ", "(pas de message)", "Rien n'a ete traite."


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
    except ToolFailure:
        pass
    return {}


async def ask(state: Thread):
    """The cabinet's agent, in the thread's own conversation (its session id as thread id)."""
    thread = state["thread"]
    text = (thread["message"] or NO_TEXT) + (FILES_NOTE + ", ".join(thread["files"]) if thread["files"] else "")
    thread_id = thread["session_id"]  # the email thread's own session: its conversation, its files
    config = {"configurable": {"thread_id": thread_id, "session_id": thread["session_id"], "cabinet": state["cabinet"]}}
    try:
        result = await agent.make_agent(config).ainvoke({"messages": [HumanMessage(text)]}, config=config)
        return {"reply": result["messages"][-1].text}
    except Exception as error:  # no reply (the model failing): set aside, with why, never asked twice
        return {"error": f"Tarik n'a pas pu repondre pour le moment. {type(error).__name__}"}


async def refusal(state: Thread):
    result = await call(state["cabinet"], "thread_refusal", thread_id=state["thread"]["thread_id"])
    return {"refusal": result}


async def notify(state: Thread):
    found = state["refusal"]
    await call(state["cabinet"], "notify_manager", sender=found["sender"], subject=found["subject"], reason=found["reason"])
    return {}


async def send(state: Thread):
    thread = state["thread"]
    try:
        await call(state["cabinet"], "send_email", to=thread["to"], subject=thread["subject"], body=state["reply"], in_reply_to=thread["in_reply_to"], references=thread["references"])
    except ToolFailure as failure:
        return {"error": str(failure)}
    return {}


async def follow_up(state: Thread):
    """A follow-up: no agent, no time limit. Traitement then Dossier in mode resume; one email tells both."""
    thread = state["thread"]
    fixed = {"mode": "resume", "session_id": thread["session_id"], "cabinet": state["cabinet"]}
    summaries = []
    for graph in (traitement.graph, dossier.graph):
        result = await graph.ainvoke({"call": fixed, "outcomes": []})
        if result["message"] != NOTHING:
            summaries.append(result["message"])
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
fil.add_conditional_edges("refus", lambda s: "manager" if s["refusal"].get("refused") else "reponse", ["manager", "reponse"])
fil.add_edge("manager", "fin")
fil.add_edge("reponse", "fin")
fil.add_edge("suite", "reponse")
fil.add_edge("fin", END)
FIL = fil.compile(name="fil")

run = StateGraph(Run)
run.add_node("fils", claim)
run.add_node("fil", FIL)
run.add_edge(START, "fils")
run.add_conditional_edges("fils", each_thread, ["fil", END])
run.add_edge("fil", END)
graph = run.compile(name="Tarik - Emails")
