"""Traitement as a graph: the fixed processing sequence of the documents a client sends.

Where a message goes comes from the database (route_message): files (the dossier found from the name the client
gave, then the conversation's new files, from identification to verified drafts, posted only when the client asked
to; with none, the questions other conversations left on the dossier are asked again and the drafts left waiting,
held back or stopped are resumed), resume (waiting drafts only), answer (the client's answer, then the sequence
resumes at its next step), or both. One subgraph per document (a file received, or a draft taken up again), one per
invoice in four phases, each visible node by node; they run one after another, so two invoices of one message never
race on the same tiers or duplicate check."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine.tools import call as run_tool
from engine.tools import failed, ok_or_summary, run_step, sender_checked, started, summary
from tarik.core.conversation import mentions_attachment
from tarik.invoices.pipeline import INVOICE_CHAIN, position

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


class Given(TypedDict, total=False):
    """What an invoice's run, and each of its phases, is started with (never the outcomes before it)."""

    call: dict
    run_id: str
    entry: str  # the step the sequence starts at
    file_id: int
    invoice_id: int


class Invoice(Given, Outcomes, total=False):
    last: dict


class Done(Outcomes, total=False):
    """What a phase hands back: its own outcomes, its last one, and the invoice (created in the first phase)."""

    last: dict
    invoice_id: int


def extra(tool, call, run_id):
    """What a step is given besides its invoice."""
    words = {"client_message": call.get("client_message") or ""}
    return {
        "wait_until_read": {"run_id": run_id},
        "correct_invoice": {"payment_method": call.get("mode_paiement") or "", **words},
        "classify_invoice": {"invoice_type": call.get("invoice_type") or ""},
        "assign_account": {"answer": call.get("compte") or "", "session_id": call["session_id"], **words},
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
        result = await run_step(state, tool, **ids, **extra(tool, call, state["run_id"]))
        return {"last": result, "invoice_id": result.get("invoice_id", state.get("invoice_id")), "outcomes": [result]}

    return node


# An invoice's steps in four phases, each folded into one box on the canvas: the same steps, in the same order
# (INVOICE_CHAIN, the tools' own), only grouped so a person can follow an invoice phase by phase.
PHASES = {
    "lecture": ["create_draft_invoice", "wait_until_read", "check_invoice", "correct_invoice"],
    "classement": ["classify_invoice", "link_tiers", "check_duplicate"],
    "comptes": [
        "assign_account",
        "settle_tva",
        "check_journal",
        "settle_withholding",
        "settle_prepaid",
        "settle_exercice",
    ],
    "comptabilisation": ["configure_cabinet", "hold_posting", "post_invoice"],
}
assert [tool for steps in PHASES.values() for tool in steps] == INVOICE_CHAIN, "the phases must hold the chain in order"


def ok_then(following):
    """Each step (and phase) only when the one before says ok; a question, stop, wait or error ends the invoice's run.
    Its two ways out named, so the drawn graph shows the sequence (LangGraph cannot see inside the lambda)."""
    return (lambda s: following if s["last"].get("status") == "ok" else END), list({following, END})


def phase(name, steps):
    """One phase: its steps in order, started at the run's entry step when it is one of them, else at its first."""
    graph = StateGraph(Invoice, input_schema=Given, output_schema=Done)
    for index, tool in enumerate(steps):
        graph.add_node(tool, invoice_step(tool))
        graph.add_conditional_edges(tool, *ok_then(steps[index + 1] if index + 1 < len(steps) else END))
    graph.add_conditional_edges(START, lambda s: s["entry"] if s["entry"] in steps else steps[0], steps)
    return graph.compile(name=name)


invoice = StateGraph(Invoice, input_schema=Given, output_schema=Outcomes)
names = list(PHASES)
for index, name in enumerate(names):
    invoice.add_node(name, phase(name, PHASES[name]))
    invoice.add_conditional_edges(name, *ok_then(names[index + 1] if index + 1 < len(names) else END))
invoice.add_conditional_edges(START, lambda s: next(n for n in names if s["entry"] in PHASES[n]), names)
INVOICE = invoice.compile(name="facture")


class File(Given, Outcomes, total=False):
    """A document: a file just received (file_id), or a draft taken up again (invoice_id, entry)."""

    last: dict


async def identify(state: File):
    message = state["call"].get("client_message") or ""
    result = await run_step(state, "identify_document", file_id=state["file_id"], client_message=message)
    return {"last": result, "outcomes": [result]}


async def import_statement(state: File):
    call = state["call"]
    result = await run_step(
        state,
        "import_bank_statement",
        file_id=state["file_id"],
        dossier_id=call["dossier_id"],
        client_message=call.get("client_message") or "",
    )
    return {"last": result, "outcomes": [result]}


async def split(state: File):
    call = state["call"]
    result = await run_step(
        state,
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


def split_drafts(state: File):
    """The drafts Experio split off the document (a bill holding several: wait_until_read's split_off), each through
    its invoice's steps once, after the draft Tarik made."""
    done = {outcome.get("invoice_id") for outcome in state["outcomes"]}
    found = [i for outcome in state["outcomes"] for i in outcome.get("split_off") or [] if i not in done]
    return [
        Send(
            "facture",
            {
                "call": state["call"],
                "run_id": state["run_id"],
                "entry": "wait_until_read",
                "invoice_id": invoice_id,
                "outcomes": [],
            },
        )
        for invoice_id in dict.fromkeys(found)
    ] or END


file = StateGraph(File, output_schema=Outcomes)
file.add_node("identification", identify)
file.add_node("releve", import_statement)
file.add_node("decoupage", split)
file.add_node("facture", INVOICE)
# A new file is identified first; a draft taken up again goes straight to its invoice's steps.
file.add_conditional_edges(
    START, lambda s: "facture" if s.get("entry") else "identification", ["identification", "facture"]
)
file.add_conditional_edges("identification", after_identify, ["releve", "decoupage", END])
file.add_conditional_edges("decoupage", drafts, ["facture", END])
file.add_conditional_edges("facture", split_drafts, ["facture", END])
FILE = file.compile(name="fichier")  # a document: drawn once, whatever brought it


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
    if route == "answer" and call.get("comptabiliser") == "oui":
        route = "both"  # they ask to post: posting happens, whatever question waits (their words may answer it too)
    return {"route": route if route in ("files", "answer", "resume", "both") else "files"}


def by_route(state: Run):
    return {"answer": "reponse", "resume": "brouillons", "both": ["reponse", "dossier"]}.get(state["route"], "dossier")


def with_dossier(state: Run):
    """The call, with the dossier the run works in: what the file and invoice steps are given."""
    return {**state["call"], "dossier_id": state.get("dossier_id", 0)}


async def dossier_found(state: Run):
    """The dossier the client named, found here (not by the agent: it never sees an id). A name that is no single
    dossier ends the run: the summary asks the client."""
    result = await run_step(state, "resolve_dossier", name=state["call"].get("dossier") or "")
    return {"dossier_id": result.get("dossier_id", 0), "outcomes": [result]}


async def sender(state: Run):
    return await sender_checked(state, state["dossier_id"])


async def files(state: Run):
    # Reached only from a client's message (a follow-up resumes its drafts): no file is told to the client when they
    # said they attached one (an answer or « comptabilisez » carries none, and a zip's left out files are told).
    asked = mentions_attachment(state["call"].get("client_message") or "")
    result = await run_step(state, "files_to_process", session_id=state["call"]["session_id"], asked=asked)
    return {"files": result.get("files", []), "outcomes": [result]}


def each_file(state: Run):
    """One file run per new file; none: the drafts left waiting. Only this conversation's questions are asked in it,
    never those other conversations left on the dossier."""
    if failed(state):
        return "resume"  # the files could not be listed: recorded, nothing more
    if not state["files"]:
        return "brouillons"
    return [
        Send("fichier", {"call": with_dossier(state), "run_id": state["run_id"], "file_id": file_id, "outcomes": []})
        for file_id in state["files"]
    ]


async def waiting(state: Run):
    """The drafts to take up again. What the client asks (not a follow-up) also resumes the drafts a step stopped,
    and, with "oui", the dossier's drafts held back, from any conversation."""
    call = state["call"]
    result = await run_step(
        state,
        "waiting_drafts",
        session_id=call["session_id"],
        held=call.get("comptabiliser") == "oui",
        dossier_id=state.get("dossier_id", 0),
        stopped=call.get("mode") != "resume",
        run_id=state["run_id"],
        invoice_type=call.get("invoice_type") or "",
    )
    return {"invoices": result.get("invoices", []), "outcomes": [result]}


def each_draft(state: Run):
    """Each draft to take up again goes back to the reading step."""
    if not state.get("invoices"):
        return "resume"
    return [
        Send(
            "fichier",
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
    result = await run_step(
        state,
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
    if following == "identify_document":  # the client said what the file is: it starts over from there
        call = {**state["call"], "dossier_id": last["dossier_id"], "invoice_type": last.get("invoice_type") or ""}
        return Send("fichier", {"call": call, "run_id": state["run_id"], "file_id": last["file_id"], "outcomes": []})
    # An account the client corrected with their answer: the invoice goes back to its account step, past it already.
    if state["call"].get("compte") and following in INVOICE_CHAIN and position(following) > position("assign_account"):
        following = "assign_account"
    return Send(
        "fichier",
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
run.add_node("brouillons", waiting)
run.add_node("reponse", answer)
run.add_node("resume", summary)
run.add_edge(START, "debut")
# A failed save is recorded and goes no further: the files the run would work on are missing.
run.add_conditional_edges("debut", *ok_or_summary("aiguillage"))
run.add_conditional_edges("aiguillage", by_route, ["dossier", "reponse", "brouillons"])
run.add_conditional_edges("dossier", *ok_or_summary("expediteur"))
run.add_conditional_edges("expediteur", *ok_or_summary("fichiers"))
run.add_conditional_edges("fichiers", each_file, ["fichier", "brouillons", "resume"])
run.add_conditional_edges("brouillons", each_draft, ["fichier", "resume"])
run.add_conditional_edges("reponse", resume_at, ["fichier", "brouillons", "resume"])
run.add_edge("fichier", "resume")
run.add_edge("resume", END)
# One task at a time: two invoices of one new supplier never both create
# it (link_tiers), and the second of two identical invoices sees the first (check_duplicate).
graph = run.compile(name="Tarik - Traitement").with_config(max_concurrency=1)
