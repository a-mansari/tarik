"""Traitement as a graph: the fixed processing sequence of the documents a client sends.

Where a message goes comes from the database (route_message): files (the dossier found from the name the client
gave, then the conversation's new files, from identification to verified drafts, posted only when the client asked
to; with none, the questions other conversations left on the dossier are asked again and the drafts left waiting,
held back or stopped are resumed), resume (waiting drafts only), answer (the client's answer, then the sequence
resumes at its next step), or both. One subgraph per file, one per invoice, each visible node by node; they run one
after another, so two invoices of one message never race on the same tiers or duplicate check."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine.tools import call as run_tool
from engine.tools import sender_checked, started, step, summary
from tarik.invoices.pipeline import INVOICE_CHAIN

INPUTS = [
    "mode",
    "session_id",
    "dossier",
    "answer",
    "decision",
    "about",
    "invoice_type",
    "client_message",
    "tva_non_recuperable",
    "retenue",
    "repartir",
    "compte",
    "mode_paiement",
    "exercice",
    "comptabiliser",
    "attachments",
    "cabinet",
]


class Outcomes(TypedDict):
    outcomes: Annotated[list, operator.add]  # every step's result, in the order it ended


class Run(Outcomes, total=False):
    call: dict  # the "Appel" inputs (INPUTS)
    run_id: str
    route: str  # where route_message sent the message: files, answer, resume or both
    dossier_id: int  # the dossier found from the name the client gave (resolve_dossier)
    files: list
    invoices: list
    message: str  # the run's summary: the workflow's answer


class Invoice(Outcomes, total=False):
    call: dict
    run_id: str
    entry: str  # the step the sequence starts at
    file_id: int
    invoice_id: int
    last: dict


def extra(tool, call, run_id):
    """What a step is given besides its invoice."""
    words = {"client_message": call.get("client_message") or ""}
    return {
        "wait_until_read": {"run_id": run_id},
        "correct_invoice": {"payment_method": call.get("mode_paiement") or "", **words},
        "classify_invoice": {"invoice_type": call.get("invoice_type") or ""},
        "assign_account": {"answer": call.get("compte") or "", **words},
        "settle_tva": {"answer": call.get("tva_non_recuperable") or ""},
        "settle_withholding": {"answer": call.get("retenue") or "", **words},
        "settle_prepaid": {"answer": call.get("repartir") or ""},
        "settle_exercice": {"answer": call.get("exercice") or "", "decision": call.get("exercice") or ""},
        "hold_posting": {"session_id": call["session_id"], "answer": call.get("comptabiliser") or ""},
    }.get(tool, {})


def invoice_step(tool):
    """The node running ``tool`` on the invoice: its outcome recorded, kept as ``last`` for the routing."""

    async def node(state: Invoice):
        call = state["call"]
        if tool == "create_draft_invoice":
            ids = {"file_id": state["file_id"], "dossier_id": call["dossier_id"]}
        else:
            ids = {"invoice_id": state["invoice_id"]}
        result = await step(
            call["cabinet"], state["run_id"], call["session_id"], tool, **ids, **extra(tool, call, state["run_id"])
        )
        return {"last": result, "invoice_id": result.get("invoice_id", state.get("invoice_id")), "outcomes": [result]}

    return node


invoice = StateGraph(Invoice, output_schema=Outcomes)
for index, tool in enumerate(INVOICE_CHAIN):  # the tools' own order (tarik/invoices/pipeline.py)
    invoice.add_node(tool, invoice_step(tool))
    following = INVOICE_CHAIN[index + 1] if index + 1 < len(INVOICE_CHAIN) else END
    # Each step only when the previous one says ok; a question, stop, wait or error ends the invoice's run.
    # Its two ways out named, so the drawn graph shows the sequence (LangGraph cannot see inside the lambda).
    invoice.add_conditional_edges(
        tool, lambda s, f=following: f if s["last"].get("status") == "ok" else END, list({following, END})
    )
invoice.add_conditional_edges(START, lambda s: s["entry"], INVOICE_CHAIN)
INVOICE = invoice.compile(name="facture")


class File(Outcomes, total=False):
    call: dict
    run_id: str
    file_id: int
    last: dict


async def identify(state: File):
    result = await step(
        state["call"]["cabinet"],
        state["run_id"],
        state["call"]["session_id"],
        "identify_document",
        file_id=state["file_id"],
    )
    return {"last": result, "outcomes": [result]}


async def import_statement(state: File):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "import_bank_statement",
        file_id=state["file_id"],
        dossier_id=call["dossier_id"],
    )
    return {"last": result, "outcomes": [result]}


async def split(state: File):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "split_invoices",
        file_id=state["file_id"],
        invoice_type=call.get("invoice_type") or "",
        client_message=call.get("client_message") or "",
    )
    return {"last": result, "outcomes": [result]}


def after_identify(state: File):
    last = state["last"]
    if last.get("status") != "ok":
        return END
    return "releve" if last.get("kind") == "statement" else "decoupage"


def drafts(state: File):
    """One invoice run per invoice cut out of the file, from its draft."""
    if state["last"].get("status") != "ok":
        return END
    return [
        Send(
            "facture",
            {
                "call": state["call"],
                "run_id": state["run_id"],
                "entry": "create_draft_invoice",
                "file_id": file_id,
                "outcomes": [],
            },
        )
        for file_id in state["last"]["files"]
    ]


file = StateGraph(File, output_schema=Outcomes)
file.add_node("identification", identify)
file.add_node("releve", import_statement)
file.add_node("decoupage", split)
file.add_node("facture", INVOICE)
file.add_edge(START, "identification")
file.add_conditional_edges("identification", after_identify, ["releve", "decoupage", END])
file.add_conditional_edges("decoupage", drafts, ["facture", END])
FILE = file.compile(name="fichier")


async def aiguillage(state: Run):
    """Where the message goes, from what the database knows (route_message); without it, the mode decides."""
    call = state["call"]
    try:
        route = (
            await run_tool(
                call["cabinet"], "route_message", session_id=call["session_id"], mode=call.get("mode") or "files"
            )
        )["route"]
    except Exception:
        route = call.get("mode")
    return {"route": route if route in ("files", "answer", "resume", "both") else "files"}


def by_route(state: Run):
    return {"answer": "reponse", "resume": "brouillons", "both": ["reponse", "dossier"]}.get(state["route"], "dossier")


def with_dossier(state: Run):
    """The call, with the dossier the run works in: what the file and invoice steps are given."""
    return {**state["call"], "dossier_id": state.get("dossier_id", 0)}


async def dossier_found(state: Run):
    """The dossier the client named, found here (not by the agent: it never sees an id). A name that is no single
    dossier ends the run: the summary asks the client."""
    call = state["call"]
    result = await step(
        call["cabinet"], state["run_id"], call["session_id"], "resolve_dossier", name=call.get("dossier") or ""
    )
    return {"dossier_id": result.get("dossier_id", 0), "outcomes": [result]}


async def sender(state: Run):
    return await sender_checked(state, state["dossier_id"])


async def files(state: Run):
    call = state["call"]
    result = await step(
        call["cabinet"], state["run_id"], call["session_id"], "files_to_process", session_id=call["session_id"]
    )
    return {"files": result.get("files", []), "outcomes": [result]}


def each_file(state: Run):
    """One file run per new file; none: the dossier's waiting questions, then the drafts left waiting."""
    if state["outcomes"][-1].get("status") != "ok":
        return "resume"  # the files could not be listed: recorded, nothing more
    if not state["files"]:
        return "questions"
    return [
        Send("fichier", {"call": with_dossier(state), "run_id": state["run_id"], "file_id": file_id, "outcomes": []})
        for file_id in state["files"]
    ]


async def questions(state: Run):
    """The questions other conversations left on this dossier's invoices, asked again here: each recorded as this
    run's question, which makes it this conversation's (record_step), so the summary asks it and the answer finds it."""
    call = state["call"]
    found = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "dossier_questions",
        session_id=call["session_id"],
        dossier_id=state["dossier_id"],
    )
    for question in found.get("questions", []):
        await run_tool(
            call["cabinet"],
            "record_step",
            run_id=state["run_id"],
            session_id=call["session_id"],
            origin="dossier_questions",
            result={"status": "question", **question},
        )
    return {}


async def waiting(state: Run):
    """The drafts to take up again. What the client asks (not a follow-up) also resumes the drafts a step stopped,
    and, with "oui", the dossier's drafts held back, from any conversation."""
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "waiting_drafts",
        session_id=call["session_id"],
        held=call.get("comptabiliser") == "oui",
        dossier_id=state.get("dossier_id", 0),
        stopped=call.get("mode") != "resume",
    )
    return {"invoices": result.get("invoices", []), "outcomes": [result]}


def each_draft(state: Run):
    """Each draft to take up again goes back to the reading step."""
    if not state.get("invoices"):
        return "resume"
    return [
        Send(
            "facture",
            {
                "call": with_dossier(state),
                "run_id": state["run_id"],
                "entry": "wait_until_read",
                "invoice_id": invoice_id,
                "outcomes": [],
            },
        )
        for invoice_id in state["invoices"]
    ]


async def answer(state: Run):
    call = state["call"]
    result = await step(
        call["cabinet"],
        state["run_id"],
        call["session_id"],
        "answer_pending",
        session_id=call["session_id"],
        answer=call.get("answer") or call.get("client_message") or "",
        decision=str(call.get("decision") or ""),
        about=str(call.get("about") or ""),
    )
    return {"outcomes": [result]}


def resume_at(state: Run):
    """Where the sequence resumes after the answer: its next step, the waiting drafts, or nowhere."""
    last = state["outcomes"][-1]
    following = last.get("next_step")
    if last.get("status") != "ok" or not following:
        return "resume"
    if following == "waiting_drafts":
        return "brouillons"
    return Send(
        "facture",
        {
            "call": with_dossier(state),
            "run_id": state["run_id"],
            "entry": following,
            "invoice_id": last["invoice_id"],
            "outcomes": [],
        },
    )


run = StateGraph(Run)
run.add_node("debut", started(INPUTS))
run.add_node("aiguillage", aiguillage)
run.add_node("dossier", dossier_found)
run.add_node("expediteur", sender)
run.add_node("fichiers", files)
run.add_node("fichier", FILE)
run.add_node("questions", questions)
run.add_node("brouillons", waiting)
run.add_node("facture", INVOICE)
run.add_node("reponse", answer)
run.add_node("resume", summary)
run.add_edge(START, "debut")
# A failed save is recorded and goes no further: the files the run would work on are missing.
run.add_conditional_edges(
    "debut", lambda s: "aiguillage" if s["outcomes"][-1].get("status") == "ok" else "resume", ["aiguillage", "resume"]
)
run.add_conditional_edges("aiguillage", by_route, ["dossier", "reponse", "brouillons"])
run.add_conditional_edges(
    "dossier", lambda s: "expediteur" if s["outcomes"][-1].get("status") == "ok" else "resume", ["expediteur", "resume"]
)
run.add_conditional_edges(
    "expediteur", lambda s: "fichiers" if s["outcomes"][-1].get("status") == "ok" else "resume", ["fichiers", "resume"]
)
run.add_conditional_edges("fichiers", each_file, ["fichier", "questions", "resume"])
run.add_edge("questions", "brouillons")
run.add_conditional_edges("brouillons", each_draft, ["facture", "resume"])
run.add_conditional_edges("reponse", resume_at, ["facture", "brouillons", "resume"])
run.add_edge("fichier", "resume")
run.add_edge("facture", "resume")
run.add_edge("resume", END)
# One task at a time: two invoices of one new supplier never both create
# it (link_tiers), and the second of two identical invoices sees the first (check_duplicate).
graph = run.compile(name="Tarik - Traitement").with_config(max_concurrency=1)
