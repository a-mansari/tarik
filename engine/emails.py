"""The Emails graph: claim the cabinet's email threads waiting for an answer and answer each one.

Each claimed thread is a run of its own (FIL, "Fil d'emails"), all side by side: acknowledge it, ask the cabinet's
agent in the thread's own conversation (engine.chat.answered, which reminds it of files still untaken), the client
told every few minutes it still works when the cabinet chose so (kept_informed), check the sender (thread_refusal)
and send the reply (send_email only sends to whitelisted addresses). A refused sender gets nothing: the manager is
notified and the thread stays set aside. A follow-up thread skips the agent: it runs Traitement then Dossier in mode
resume and sends their summaries."""

import asyncio
import contextlib
import operator
import re
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.calls import ToolFailure, call, stops_at, within
from engine.chat import answered
from engine.tools.core.conversation import for_client, left_out
from engine.tools.dossiers.behaviour import behaviour
from engine.tools.emails.inbox import LEASE_MINUTES, send_progress
from engine.tools.invoices.pipeline import NOTHING


class Run(TypedDict, total=False):
    cabinet: str
    threads: list
    done: Annotated[list, operator.add]


class Steps(TypedDict, total=False):
    """The FIL graph's output keys: what each thread step reports, one key each.

    A graph's output keys are what its steps show in the run's step-by-step record. None of these reach the Emails
    run except ``done``."""

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
    """Graph node: claim the cabinet's threads waiting for an answer (claim_threads) into "threads"."""
    result = await call(state["cabinet"], "claim_threads")
    claimed = time.time()  # their lease starts now (inbox.LEASE_MINUTES)
    return {"threads": [{**thread, "claimed": claimed} for thread in result.get("threads", [])]}


async def each_thread(state: Run):
    """Graph node: run FIL for each claimed thread, side by side, each as a run of its own, and collect their "done".

    Separate conversations, each from its own sender, are never drawn on one canvas. A thread run that raises
    adds nothing to "done", leaves the others running, and is claimed again later."""
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
    """Route a thread: "suite_documents" for a follow-up, else "accuse" (acknowledge, then the agent)."""
    return "suite_documents" if state["thread"]["follow_up"] else "accuse"


async def acknowledge(state: Thread):
    """Graph node: acknowledge the thread (acknowledge_thread); when that fails, the agent still answers."""
    try:
        result = await call(state["cabinet"], "acknowledge_thread", thread_id=state["thread"]["thread_id"])
        return {"acknowledged": said(result)}
    except Exception as error:  # not sent (whitelist, SMTP): the agent answers all the same
        return {"acknowledged": f"Pas envoyé : {failure_text(error)}"}


# What a reply or forward puts before a subject: « Re: », « TR : », « Fwd: », repeated or not.
REPLY_PREFIX = re.compile(r"^(?:\s*(?:re|tr|fw|fwd|ref)\s*:)+\s*", re.IGNORECASE)


def with_subject(subject, message):
    """Return the client's ``message`` headed by "Objet : <subject>", reply prefixes (Re:, TR:, Fwd:...) removed;
    ``message`` alone when there is no subject. The subject is the client's words too (« Facture achat » gives the
    side)."""
    subject = REPLY_PREFIX.sub("", subject or "").strip()
    return f"Objet : {subject}\n\n{message}" if subject else message


async def kept_informed(cabinet, thread, work):
    """Await ``work`` and return its result, emailing the client in ``thread`` (send_progress) each time the
    cabinet's progress minutes pass without it done; never when they are 0, nor when the setting cannot be read. A
    progress email not sent is skipped. It is sent outside the cabinet's tool slots (within): the agents' own calls
    never wait behind it. None is sent once LEASE_MINUTES have passed since the thread was claimed ("claimed", for
    every step of its run): it is then taken up again, by a run that keeps its own client informed."""
    deadline = thread.get("claimed", time.time()) + LEASE_MINUTES * 60
    task = asyncio.ensure_future(work)
    try:
        try:
            minutes = await within(cabinet, behaviour, "progress")
        except Exception:  # an optional email: the answer goes on without it
            minutes = 0
        while minutes and time.time() + minutes * 60 < deadline:
            if (await asyncio.wait({task}, timeout=minutes * 60))[0]:
                break
            with contextlib.suppress(Exception):  # not sent (whitelist, SMTP): the reply still comes
                await within(cabinet, send_progress, thread["thread_id"])
        return await task
    finally:
        task.cancel()  # the thread's run cancelled: its agent too


async def ask(state: Thread):
    """Graph node: set "reply" to the cabinet's agent's answer in the thread's own conversation (engine.chat.answered),
    the client kept informed meanwhile (kept_informed), or "error" when the agent failed (the thread is set aside,
    never asked twice)."""
    thread = state["thread"]
    try:
        message = with_subject(thread.get("subject"), thread["message"])
        given = state["cabinet"], thread["session_id"], message, thread["files"]
        work = answered(*given, "tarik", thread.get("skipped", ()))
        reply = await kept_informed(state["cabinet"], thread, work)
        return {"reply": reply}
    except Exception:  # no reply (the model failing): set aside, with why, never asked twice
        return {"error": "Je n'ai pas pu répondre pour le moment. J'ai mis ce fil de côté, sans réponse au client."}


async def refusal(state: Thread):
    """Graph node: set "refusal" to whether the sender is refused (thread_refusal), or "error" when that failed."""
    try:
        return {"refusal": await call(state["cabinet"], "thread_refusal", thread_id=state["thread"]["thread_id"])}
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}


async def notify(state: Thread):
    """Graph node: notify the cabinet's manager of a refused sender (notify_manager); the sender gets nothing."""
    found = state["refusal"]
    try:
        told = await call(
            state["cabinet"], "notify_manager", sender=found["sender"], subject=found["subject"], reason=found["reason"]
        )
        return {"manager": said(told)}
    except Exception as error:  # no manager set, or the email not sent: the sender still gets nothing
        return {"manager": f"Pas prévenu : {failure_text(error)}"}


def said(result):
    """Return a tool's result as one text: a dict's "message" (else the dict as text), or the text itself."""
    return result.get("message", str(result)) if isinstance(result, dict) else str(result)


def failure_text(error):
    """Return why a thread is set aside: a ToolFailure's message, else "Erreur (<exception class>)."."""
    return str(error) if isinstance(error, ToolFailure) else f"Erreur ({type(error).__name__})."


async def send(state: Thread):
    """Graph node: email the reply (plus what a zip left out) in the thread (send_email), with files made in the run;
    sets "sent" and "sent_id", or "error"."""
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
    return {"sent": f"Envoyé à {thread['to']}.", "sent_id": (result or {}).get("message_id") or ""}


async def resumed(state, name, node):
    """Run workflow ``name`` in mode resume for the thread's conversation, as a run nested under ``node``, and return
    its summary; "" when it had nothing to resume."""
    from engine import runs  # runs imports the graphs, this one among them

    thread = state["thread"]
    fixed = {"mode": "resume", "session_id": thread["session_id"], "cabinet": state["cabinet"]}
    work = runs.nested(name, {"call": fixed, "outcomes": []}, state["cabinet"], thread["session_id"], node=node)
    result = await kept_informed(state["cabinet"], thread, work)
    return "" if result["message"] == NOTHING else result["message"]


async def follow_up_documents(state: Thread):
    """Graph node (follow-up, no agent, no time limit): run Traitement in mode resume for the drafts left waiting;
    adds its summary to "summaries", or sets "error"."""
    try:
        said = await resumed(state, "traitement", "suite_documents")
        return {"summaries": [said] if said else []}
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}


async def follow_up_dossiers(state: Thread):
    """Graph node: run Dossier in mode resume (the dossiers left to finish) and set "reply" to both summaries in one
    email, or "error"."""
    try:
        summaries = [*state.get("summaries", []), await resumed(state, "dossier", "suite_dossiers")]
    except Exception as error:  # set aside with why: never answered blind
        return {"error": failure_text(error)}
    told = "\n\n".join(said for said in summaries if said)
    return {"reply": for_client("J'ai poursuivi le traitement de vos documents. Voici où il en est.\n\n" + told)}


async def finish(state: Thread):
    """Graph node: close the thread (finish_thread), as sent or set aside with the error as reason; adds it to
    "done"."""
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


@stops_at("fin")
def after_refusal(state: Thread):
    """Route: "manager" for a refused sender, else "reponse"; "fin" when the refusal check failed."""
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
on_error = stops_at("fin")  # an error ends the thread: kept off the canvas
fil.add_conditional_edges("tarik", on_error(lambda s: "fin" if s.get("error") else "refus"), ["fin", "refus"])
fil.add_conditional_edges("refus", after_refusal, ["fin", "manager", "reponse"])
fil.add_edge("manager", END)  # thread_refusal set the thread aside and counted its emails: finish_thread would undo it
fil.add_edge("reponse", "fin")
fil.add_conditional_edges(
    "suite_documents", on_error(lambda s: "fin" if s.get("error") else "suite_dossiers"), ["fin", "suite_dossiers"]
)
fil.add_conditional_edges(
    "suite_dossiers", on_error(lambda s: "fin" if s.get("error") else "reponse"), ["fin", "reponse"]
)
fil.add_edge("fin", END)
FIL = fil.compile(name="fil")

run = StateGraph(Run)
run.add_node("fils", claim)
run.add_node("fil", each_thread)
run.add_edge(START, "fils")
run.add_conditional_edges("fils", lambda s: "fil" if s["threads"] else END, ["fil", END])
run.add_edge("fil", END)
graph = run.compile(name="Tarik - Emails")
