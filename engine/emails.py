"""Emails as a graph: for the cabinet named, each thread waiting for an answer is claimed, then in a run of its own
(FIL, "Fil d'emails", one per thread, side by side) acknowledged, answered
by the cabinet's agent (the agent graph, in the thread's own conversation; reminded of files still untaken), and the
reply sent to whitelisted addresses. A sender refused (check_sender) gets nothing: the manager is told, the thread
stays set aside. A follow-up thread runs Traitement then Dossier in mode resume and sends their summaries."""

import asyncio
import operator
import time
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from engine.tools import ToolFailure, call, within
from tarik.core.conversation import FILES_NOTE, NO_TEXT, remember_client_message
from tarik.emails.inbox import untaken
from tarik.invoices.pipeline import NOTHING

# Files no tool took after the reply mean the files tool did not run (tarik.emails.inbox.untaken): the agent is
# asked again with a reminder, ASK_TRIES asks at most, none after QUICK seconds; the last reply is sent.
ASK_TRIES, QUICK = 3, 200
REMINDER = "\n\nRappel : ces fichiers n'ont pas ete traites. Traitez-les maintenant si rien ne manque."


class Run(TypedDict, total=False):
    cabinet: str
    threads: list
    done: Annotated[list, operator.add]


class Steps(TypedDict, total=False):
    """What a thread's steps say, each in its own key: kept in a run's step-by-step record (a graph's output keys
    are what its steps show), and none of it the Emails run's, but done."""

    acknowledged: str
    reply: str
    error: str
    refusal: dict
    manager: str
    sent: str
    done: Annotated[list, operator.add]


class Thread(Steps, total=False):
    cabinet: str  # not an output: threads run side by side, each would write it at once
    thread: dict


async def claim(state: Run):
    result = await call(state["cabinet"], "claim_threads")
    return {"threads": result.get("threads", [])}


async def each_thread(state: Run):
    """Each thread claimed, a run of its own ("Fil d'emails", FIL): separate conversations, each from its own sender,
    are never drawn on one canvas. They run side by side; one failing leaves the others, and is claimed again later."""
    from engine import runs  # runs imports the graphs, this one among them

    cabinet = state["cabinet"]
    ran = await asyncio.gather(
        *(
            runs.nested(
                "fil", {"cabinet": cabinet, "thread": thread, "done": []}, cabinet, thread["session_id"], node="fil"
            )
            for thread in state["threads"]
        ),
        return_exceptions=True,
    )
    return {"done": [entry for result in ran if isinstance(result, dict) for entry in result.get("done", [])]}


def kind(state: Thread):
    return "suite" if state["thread"]["follow_up"] else "accuse"


async def acknowledge(state: Thread):
    try:
        return {
            "acknowledged": said(
                await call(state["cabinet"], "acknowledge_thread", thread_id=state["thread"]["thread_id"])
            )
        }
    except Exception as error:  # not sent (whitelist, SMTP): the agent answers all the same
        return {"acknowledged": f"Pas envoye : {failure_text(error)}"}


async def ask(state: Thread):
    """The cabinet's agent, in the thread's own conversation (its session id as thread id), asked again with a
    reminder while the thread's files stay untaken. Each ask is a run of the agent's own, started from this step."""
    from engine import runs  # runs imports the graphs, this one among them

    thread, cabinet = state["thread"], state["cabinet"]
    text = (thread["message"] or NO_TEXT) + (FILES_NOTE + ", ".join(thread["files"]) if thread["files"] else "")
    session = thread["session_id"]  # the email thread's own session: its conversation (the agent's thread), its files
    started = time.monotonic()
    try:
        await within(cabinet, remember_client_message, thread["session_id"], thread["message"])
        for attempt in range(ASK_TRIES):
            asked = {"messages": [HumanMessage(text + (REMINDER if attempt else ""))]}
            result = await runs.nested("agent", asked, cabinet, session, thread=session, node="tarik")
            reply = result["messages"][-1].text
            if (
                not thread["files"]
                or time.monotonic() - started > QUICK
                or not await within(cabinet, untaken, thread["session_id"])
            ):
                break
        return {"reply": reply}
    except Exception:  # no reply (the model failing): set aside, with why, never asked twice
        return {"error": "Tarik n'a pas pu repondre pour le moment."}


async def refusal(state: Thread):
    try:
        return {"refusal": await call(state["cabinet"], "thread_refusal", thread_id=state["thread"]["thread_id"])}
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}


async def notify(state: Thread):
    found = state["refusal"]
    try:
        told = await call(
            state["cabinet"], "notify_manager", sender=found["sender"], subject=found["subject"], reason=found["reason"]
        )
        return {"manager": said(told)}
    except Exception as error:  # no manager set, or the email not sent: the sender still gets nothing
        return {"manager": f"Pas prevenu : {failure_text(error)}"}


def said(result):
    """A tool's answer in one line: its message, or its text."""
    return result.get("message", str(result)) if isinstance(result, dict) else str(result)


def failure_text(error):
    """Why a thread is set aside: the tool's own message, or the failure's kind."""
    return str(error) if isinstance(error, ToolFailure) else f"Erreur ({type(error).__name__})."


async def send(state: Thread):
    thread = state["thread"]
    try:
        await call(
            state["cabinet"],
            "send_email",
            to=thread["to"],
            subject=thread["subject"],
            body=state["reply"],
            in_reply_to=thread["in_reply_to"],
            references=thread["references"],
        )
    except Exception as error:
        return {"error": failure_text(error)}
    return {"sent": f"Envoye a {thread['to']}."}


async def follow_up(state: Thread):
    """A follow-up: no agent, no time limit. Traitement then Dossier in mode resume, each a run of its own started
    from this step; one email tells both."""
    from engine import runs  # runs imports the graphs, this one among them

    thread = state["thread"]
    fixed = {"mode": "resume", "session_id": thread["session_id"], "cabinet": state["cabinet"]}
    summaries = []
    try:
        for name in ("traitement", "dossier"):
            result = await runs.nested(
                name, {"call": fixed, "outcomes": []}, state["cabinet"], thread["session_id"], node="suite"
            )
            if result["message"] != NOTHING:
                summaries.append(result["message"])
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}
    return {"reply": "Suite du traitement de vos documents :\n\n" + "\n\n".join(summaries)}


async def finish(state: Thread):
    thread = state["thread"]
    sent = not state.get("error")
    await call(
        state["cabinet"],
        "finish_thread",
        thread_id=thread["thread_id"],
        message_ids=thread["message_ids"],
        sent=sent,
        reason=state.get("error", ""),
    )
    return {"done": [{"thread_id": thread["thread_id"], "sent": sent, "reason": state.get("error", "")}]}


fil = StateGraph(Thread, output_schema=Steps)
for name, node in {
    "accuse": acknowledge,
    "tarik": ask,
    "refus": refusal,
    "manager": notify,
    "reponse": send,
    "suite": follow_up,
    "fin": finish,
}.items():
    fil.add_node(name, node)
fil.add_conditional_edges(START, kind, ["accuse", "suite"])
fil.add_edge("accuse", "tarik")
fil.add_conditional_edges("tarik", lambda s: "fin" if s.get("error") else "refus", ["fin", "refus"])
fil.add_conditional_edges(
    "refus",
    lambda s: "fin" if s.get("error") else "manager" if s["refusal"].get("refused") else "reponse",
    ["fin", "manager", "reponse"],
)
fil.add_edge("manager", END)  # thread_refusal set the thread aside and counted its emails: finish_thread would undo it
fil.add_edge("reponse", "fin")
fil.add_conditional_edges("suite", lambda s: "fin" if s.get("error") else "reponse", ["fin", "reponse"])
fil.add_edge("fin", END)
FIL = fil.compile(name="fil")

run = StateGraph(Run)
run.add_node("fils", claim)
run.add_node("fil", each_thread)
run.add_edge(START, "fils")
run.add_conditional_edges("fils", lambda s: "fil" if s["threads"] else END, ["fil", END])
run.add_edge("fil", END)
graph = run.compile(name="Tarik - Emails")
