"""Emails as a graph: for the cabinet named, each thread waiting for an answer is claimed, then in a run of its own
(FIL, "Fil d'emails", one per thread, side by side) acknowledged, answered
by the cabinet's agent (the agent graph, in the thread's own conversation; reminded of files still untaken), and the
reply sent to whitelisted addresses. A sender refused (check_sender) gets nothing: the manager is told, the thread
stays set aside. A follow-up thread runs Traitement then Dossier in mode resume and sends their summaries."""

import asyncio
import operator
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import ToolFailure, call
from engine.chat import answered
from engine.tools.core.conversation import for_client, left_out
from engine.tools.invoices.pipeline import NOTHING


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
    sent_id: str  # the reply's Message-ID
    summaries: list  # a follow-up's: the resumed workflows' summaries so far
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
    thread_runs = [
        runs.nested(
            "fil", {"cabinet": cabinet, "thread": thread, "done": []}, cabinet, thread["session_id"], node="fil"
        )
        for thread in state["threads"]
    ]
    done = []
    for result in await asyncio.gather(*thread_runs, return_exceptions=True):
        if isinstance(result, dict):  # a failed thread: its exception, nothing done
            done += result.get("done", [])
    return {"done": done}


def kind(state: Thread):
    return "suite_documents" if state["thread"]["follow_up"] else "accuse"


async def acknowledge(state: Thread):
    try:
        result = await call(state["cabinet"], "acknowledge_thread", thread_id=state["thread"]["thread_id"])
        return {"acknowledged": said(result)}
    except Exception as error:  # not sent (whitelist, SMTP): the agent answers all the same
        return {"acknowledged": f"Pas envoye : {failure_text(error)}"}


# What a reply or forward puts before a subject: « Re: », « TR : », « Fwd: », repeated or not.
REPLY_PREFIX = re.compile(r"^(?:\s*(?:re|tr|fw|fwd|ref)\s*:)+\s*", re.IGNORECASE)


def with_subject(subject, message):
    """The client's ``message`` headed by its email's subject, reply prefixes taken off: the subject is the client's
    words too (« Facture achat » gives the side)."""
    subject = REPLY_PREFIX.sub("", subject or "").strip()
    return f"Objet : {subject}\n\n{message}" if subject else message


async def ask(state: Thread):
    """The cabinet's agent, in the thread's own conversation (its session id as thread id): engine.chat.answered."""
    thread = state["thread"]
    try:
        message = with_subject(thread.get("subject"), thread["message"])
        given = state["cabinet"], thread["session_id"], message, thread["files"]
        reply = await answered(*given, "tarik", thread.get("skipped", ()))
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
        result = await call(
            state["cabinet"],
            "send_email",
            to=thread["to"],
            subject=thread["subject"],
            body=state["reply"] + left_out(thread.get("skipped")),  # what a zip of theirs left out: always said
            in_reply_to=thread["in_reply_to"],
            references=thread["references"],
            attach_session=thread.get("session_id") or "",  # the files Tarik made in the run (an export)
        )
    except Exception as error:
        return {"error": failure_text(error)}
    return {"sent": f"Envoye a {thread['to']}.", "sent_id": (result or {}).get("message_id") or ""}


async def resumed(state, name, node):
    """Workflow ``name`` in mode resume for the thread's conversation, a run of its own started from ``node``: its
    summary, "" when it had nothing to resume."""
    from engine import runs  # runs imports the graphs, this one among them

    thread = state["thread"]
    fixed = {"mode": "resume", "session_id": thread["session_id"], "cabinet": state["cabinet"]}
    result = await runs.nested(name, {"call": fixed, "outcomes": []}, state["cabinet"], thread["session_id"], node=node)
    return "" if result["message"] == NOTHING else result["message"]


async def follow_up_documents(state: Thread):
    """A follow-up, no agent, no time limit: Traitement in mode resume (the drafts left waiting)."""
    try:
        said = await resumed(state, "traitement", "suite_documents")
        return {"summaries": [said] if said else []}
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}


async def follow_up_dossiers(state: Thread):
    """Then Dossier in mode resume (the dossiers left to finish); one email tells both."""
    try:
        summaries = [*state.get("summaries", []), await resumed(state, "dossier", "suite_dossiers")]
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}
    told = "\n\n".join(said for said in summaries if said)
    return {"reply": for_client("Suite du traitement de vos documents :\n\n" + told)}


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
        sent_id=state.get("sent_id", ""),
    )
    return {"done": [{"thread_id": thread["thread_id"], "sent": sent, "reason": state.get("error", "")}]}


def after_refusal(state: Thread):
    """A refused sender's manager is told; anyone else gets the reply. A refusal not read ends the thread."""
    if state.get("error"):
        return "fin"
    return "manager" if state["refusal"].get("refused") else "reponse"


fil = StateGraph(Thread, output_schema=Steps)
for name, node in {
    "accuse": acknowledge,
    "tarik": ask,
    "refus": refusal,
    "manager": notify,
    "reponse": send,
    "suite_documents": follow_up_documents,
    "suite_dossiers": follow_up_dossiers,
    "fin": finish,
}.items():
    fil.add_node(name, node)
fil.add_conditional_edges(START, kind, ["accuse", "suite_documents"])
fil.add_edge("accuse", "tarik")
fil.add_conditional_edges("tarik", lambda s: "fin" if s.get("error") else "refus", ["fin", "refus"])
fil.add_conditional_edges("refus", after_refusal, ["fin", "manager", "reponse"])
fil.add_edge("manager", END)  # thread_refusal set the thread aside and counted its emails: finish_thread would undo it
fil.add_edge("reponse", "fin")
fil.add_conditional_edges(
    "suite_documents", lambda s: "fin" if s.get("error") else "suite_dossiers", ["fin", "suite_dossiers"]
)
fil.add_conditional_edges("suite_dossiers", lambda s: "fin" if s.get("error") else "reponse", ["fin", "reponse"])
fil.add_edge("fin", END)
FIL = fil.compile(name="fil")

run = StateGraph(Run)
run.add_node("fils", claim)
run.add_node("fil", each_thread)
run.add_edge(START, "fils")
run.add_conditional_edges("fils", lambda s: "fil" if s["threads"] else END, ["fil", END])
run.add_edge("fil", END)
graph = run.compile(name="Tarik - Emails")
