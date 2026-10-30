"""The Traitement graph: the fixed processing sequence of the documents a client sends.

route_message (from the database) picks the route:
- files: find the dossier the client named, then take the conversation's new files from identification to verified
  drafts, posted only when the client asked for it. With no new file, the drafts left waiting, held back or stopped
  are taken up again.
- resume: the waiting drafts only.
- answer: pass on the client's answer, then resume the sequence at its next step.
- both: answer and files.
One subgraph per document (a file received, or a draft taken up again) and one per invoice in four phases, each
visible node by node. They run one at a time, so two invoices of one message never race on the same tiers or
duplicate check."""

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from engine.calls import call as run_tool
from engine.calls import failed, ok_or_summary, run_step, sender_checked, started, stops_at, summary
from engine.pointage import POINTING
from engine.tools.core.conversation import mentions_attachment
from engine.tools.invoices.import_folder import folder_mode
from engine.tools.invoices.pipeline import INVOICE_CHAIN, position

INPUTS = [
    "mode",
    "session_id",
    "dossier",
    "answer",
    "decision",
    "about",
    "corrections",
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
    folder_invoices: list  # the drafts an import folder made (create_import_folder), transit agent's last
    statements: list  # the conversation's statement files to take up again (waiting_drafts)
    invoices: list
    facturation: list  # the dossier's drafts from Experio's Facturation the client asked to post (waiting_drafts)
    corrections: list  # [[invoice, account]] the client gave drafts taken up again (waiting_drafts)
    message: str  # the run's summary: the workflow's answer


class Given(TypedDict, total=False):
    """The input of an invoice's subgraph and of each of its phases (never the outcomes before it)."""

    call: dict
    run_id: str
    entry: str  # the step the sequence starts at
    file_id: int
    invoice_id: int


class Invoice(Given, Outcomes, total=False):
    last: dict


class Done(Outcomes, total=False):
    """A phase's output: its own outcomes, its last result, and the invoice id (created in the first phase)."""

    last: dict
    invoice_id: int


def extra(tool, call, run_id):
    """Return the keyword arguments ``tool`` gets besides the invoice, from the run's ``call`` (the client's
    instructions) and ``run_id``; {} for a tool that takes none."""
    words = {"client_message": call.get("client_message") or ""}
    return {
        "create_draft_invoice": {
            "invoice_type": call.get("invoice_type") or call.get("scan_side") or "",
            "posting": call.get("comptabiliser") or "",
        },
        "wait_until_read": {"run_id": run_id},
        "correct_invoice": {"payment_method": call.get("mode_paiement") or "", **words},
        "classify_invoice": {"invoice_type": call.get("invoice_type") or ""},
        "assign_account": {
            "answer": call.get("compte") or "",
            "session_id": call["session_id"],
            "confirm": call.get("confirmer") is True,
            **words,
        },
        "settle_tva": {"answer": call.get("tva_non_recuperable") or ""},
        "settle_withholding": {"answer": call.get("retenue") or "", **words},
        "settle_prepaid": {"answer": call.get("repartir") or ""},
        "settle_exercice": {"answer": call.get("exercice") or "", "decision": call.get("exercice") or ""},
        "hold_posting": {"session_id": call["session_id"], "answer": call.get("comptabiliser") or ""},
    }.get(tool, {})


def invoice_step(tool):
    """Return the graph node running ``tool`` on the invoice (on the file for create_draft_invoice).

    The node records the outcome, keeps it as "last" for routing, and keeps the invoice_id the tool returns."""

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
    "comptabilisation": [
        "configure_cabinet",
        "settle_warnings",
        "split_charges",  # last write on the draft: Experio's refresh rebuilds the lines from the header
        "hold_posting",
        "post_invoice",
        "apply_credit_note",
    ],
}
assert [tool for steps in PHASES.values() for tool in steps] == INVOICE_CHAIN, "the phases must hold the chain in order"


def after(items, index):
    """Return the item after ``items[index]``; END after the last."""
    return items[index + 1] if index + 1 < len(items) else END


def ok_then(following):
    """Return (router, routes) for add_conditional_edges: go on to ``following`` only after an ok result; a question,
    stop, wait or error result ends the invoice's run (END).

    The routes are named so the drawn graph shows the sequence (LangGraph cannot see inside the lambda)."""
    router = lambda s: following if s["last"].get("status") == "ok" else END  # noqa: E731
    if following == END:  # the last step: its end is the ordinary one
        return router, [END]
    return stops_at(END)(router), [following, END]


def phase(name, steps):
    """Return the compiled subgraph of phase ``name``: its ``steps`` in order, started at the run's entry step when it
    is one of them, else at the first."""
    graph = StateGraph(Invoice, input_schema=Given, output_schema=Done)
    for index, tool in enumerate(steps):
        graph.add_node(tool, invoice_step(tool))
        graph.add_conditional_edges(tool, *ok_then(after(steps, index)))
    graph.add_conditional_edges(START, lambda s: s["entry"] if s["entry"] in steps else steps[0], steps)
    return graph.compile(name=name)


def entry_phase(state: Given):
    """Route an invoice's run to the phase holding its entry step."""
    return next(name for name, steps in PHASES.items() if state["entry"] in steps)


invoice = StateGraph(Invoice, input_schema=Given, output_schema=Outcomes)
names = list(PHASES)
for index, name in enumerate(names):
    invoice.add_node(name, phase(name, PHASES[name]))
    invoice.add_conditional_edges(name, *ok_then(after(names, index)))
invoice.add_conditional_edges(START, entry_phase, names)
INVOICE = invoice.compile(name="facture")


class File(Given, Outcomes, total=False):
    """A document's subgraph state: a file just received (file_id), or a draft or statement taken up again (entry
    with invoice_id or file_id)."""

    last: dict
    statement_id: int  # the bank statement imported from the file, pointed by POINTING
    post_asked: bool  # the client asked to post it when sending the file (kept across a question)


async def identify(state: File):
    """Graph node: identify what the file is (identify_document)."""
    message = state["call"].get("client_message") or ""
    result = await run_step(state, "identify_document", file_id=state["file_id"], client_message=message)
    return {"last": result, "outcomes": [result]}


async def import_statement(state: File):
    """Graph node: import the file as a bank statement into the dossier (import_bank_statement)."""
    call = state["call"]
    result = await run_step(
        state,
        "import_bank_statement",
        file_id=state["file_id"],
        dossier_id=call["dossier_id"],
        client_message=call.get("client_message") or "",
        posting=call.get("comptabiliser") or "",
    )
    return {"last": result, "outcomes": [result]}


async def split(state: File):
    """Graph node: cut the file into one file per invoice (split_invoices)."""
    call = state["call"]
    result = await run_step(
        state,
        "split_invoices",
        file_id=state["file_id"],
        invoice_type=call.get("invoice_type") or "",
        client_message=call.get("client_message") or "",
        dossier_id=call.get("dossier_id") or 0,
    )
    return {"last": result, "outcomes": [result]}


@stops_at(END)
def after_identify(state: File):
    """Route: "releve" for a bank statement, else "decoupage"; END when identification was not ok."""
    last = state["last"]
    if last.get("status") != "ok":
        return END
    return "releve" if last.get("kind") == "statement" else "decoupage"


def invoice_run(state: File, entry, call=None, **document):
    """Return a Send starting the invoice subgraph at step ``entry`` on ``document`` (file_id= or invoice_id=), with
    ``call`` (default: the file's call)."""
    call = call or state["call"]
    return Send("facture", {"call": call, "run_id": state["run_id"], "entry": entry, **document, "outcomes": []})


def drafts(state: File):  # its end is also an ordinary one: a file sent again, already posted
    """Route after the split: one invoice run per file cut out, from its draft's creation (scan_side, the side the
    scan shows, used when the client gave none). A file sent again whose invoice is still a draft resumes that
    draft at wait_until_read. END when the split was not ok or the file was already posted."""
    last = state["last"]
    if last.get("status") != "ok":
        return END
    if last.get("resume_invoice"):
        return [invoice_run(state, "wait_until_read", invoice_id=last["resume_invoice"])]
    call = {**state["call"], "scan_side": last["invoice_type"]} if last.get("invoice_type") else state["call"]
    if not last["files"]:
        return END  # sent again, already posted: nothing to do
    return [invoice_run(state, "create_draft_invoice", call=call, file_id=file_id) for file_id in last["files"]]


def split_drafts(state: File):
    """Route after the invoices: send the drafts Experio split off a document (wait_until_read's split_off: a bill
    holding several invoices) through the invoice steps from wait_until_read, each once; END when there are none
    left to run."""
    done = {outcome.get("invoice_id") for outcome in state["outcomes"]}
    found = []
    for outcome in state["outcomes"]:  # each draft's split-off drafts sent together, once: none of them run yet
        group = outcome.get("split_off") or []
        if group and not done & set(group):
            found += group
    if not found:
        return END
    return [invoice_run(state, "wait_until_read", invoice_id=invoice_id) for invoice_id in dict.fromkeys(found)]


async def point_statement(state: File):
    """Graph node: take the statement up once Experio has read it (settle_statement): a TPE statement is settled
    there, a bank statement's lines are pointed next (POINTING)."""
    result = await run_step(state, "settle_statement", file_id=state["file_id"], run_id=state["run_id"])
    return {
        "last": result,
        "outcomes": [result],
        "statement_id": result.get("statement_id"),
        "post_asked": bool(result.get("post_asked")),
    }


async def post_statement(state: File):
    """Graph node: post the pointed statement (post_statement)."""
    result = await run_step(state, "post_statement", file_id=state["file_id"])
    return {"last": result, "outcomes": [result]}


@stops_at(END)
def after_import(state: File):
    """Route to "pointage" when the statement is imported (now or before: an ok result with statement_id), else END."""
    last = state["last"]
    return "pointage" if last.get("status") == "ok" and last.get("statement_id") else END


def after_reading(state: File):  # its end is also an ordinary one: a TPE statement, no posting asked
    """Route after the statement is taken up: "operations" for a bank statement's lines, else as after_pointing."""
    last = state["last"]
    if last.get("status") == "ok" and last.get("bank"):
        return "operations"
    return after_pointing(state)


def after_pointing(state: File):
    """Route to "comptabilisation_releve" only after an ok pointing when the client asked to post: in this message
    (comptabiliser "oui") or when sending the statement (post_asked, kept across a question), else END."""
    asked = state["call"].get("comptabiliser") == "oui" or state.get("post_asked") is True
    return "comptabilisation_releve" if state["outcomes"][-1].get("status") == "ok" and asked else END


file = StateGraph(File, output_schema=Outcomes)
file.add_node("identification", identify)
file.add_node("releve", import_statement)
file.add_node("pointage", point_statement)
file.add_node("operations", POINTING)
file.add_node("comptabilisation_releve", post_statement)
file.add_node("decoupage", split)
file.add_node("facture", INVOICE)


# A new file is identified first; a draft taken up again goes straight to its invoice's steps, a statement taken up
# again to its pointing.
def file_start(state: File):
    """Route a document: "pointage" for a statement taken up again, "facture" for a draft, else "identification"."""
    if state.get("entry") == "statement":
        return "pointage"
    return "facture" if state.get("entry") else "identification"


file.add_conditional_edges(START, file_start, ["identification", "facture", "pointage"])
file.add_conditional_edges("identification", after_identify, ["releve", "decoupage", END])
file.add_conditional_edges("releve", after_import, ["pointage", END])
file.add_conditional_edges("pointage", after_reading, ["operations", "comptabilisation_releve", END])
file.add_conditional_edges("operations", after_pointing, ["comptabilisation_releve", END])
file.add_conditional_edges("decoupage", drafts, ["facture", END])
file.add_conditional_edges("facture", split_drafts, ["facture", END])
FILE = file.compile(name="fichier")  # a document: drawn once, whatever brought it


async def aiguillage(state: Run):
    """Graph node: set "route" (files, answer, resume or both) from route_message; when that fails, from the call's
    mode. An answer with comptabiliser "oui" becomes both; anything unknown becomes files."""
    call = state["call"]
    mode = call.get("mode") or "files"
    try:
        found = await run_tool(call["cabinet"], "route_message", session_id=call["session_id"], mode=mode)
        route = found["route"]
    except Exception:
        route = call.get("mode")
    if route == "answer" and call.get("comptabiliser") == "oui":
        route = "both"  # they ask to post: posting happens, whatever question waits (their words may answer it too)
    return {"route": route if route in ("files", "answer", "resume", "both") else "files"}


def by_route(state: Run):
    """Route: answer -> "reponse", resume -> "brouillons", both -> "reponse" and "dossier", files -> "dossier"."""
    return {"answer": "reponse", "resume": "brouillons", "both": ["reponse", "dossier"]}.get(state["route"], "dossier")


def with_dossier(state: Run):
    """Return the call with "dossier_id" set to the run's dossier (0 when none): the file and invoice runs' call."""
    return {**state["call"], "dossier_id": state.get("dossier_id", 0)}


async def dossier_found(state: Run):
    """Graph node: find the dossier the client named (resolve_dossier) and set "dossier_id". The agent never sees ids,
    so it is resolved here; a name matching no single dossier ends the run and the summary asks the client."""
    result = await run_step(state, "resolve_dossier", name=state["call"].get("dossier") or "")
    return {"dossier_id": result.get("dossier_id", 0), "outcomes": [result]}


async def sender(state: Run):
    """Graph node: check the sender may work on the found dossier (check_sender)."""
    return await sender_checked(state, state["dossier_id"])


async def files(state: Run):
    """Graph node: set "files" to the conversation's new files (files_to_process)."""
    # Reached only from a client's message (a follow-up resumes its drafts): no file is told to the client when they
    # said they attached one (an answer or « comptabilisez » carries none, and a zip's left out files are told).
    # One saved by this run did arrive, even when a step took it as something else (a supplier's attestation).
    saved = any(outcome.get("files") for outcome in state["outcomes"][:1])
    asked = mentions_attachment(state["call"].get("client_message") or "") and not saved
    result = await run_step(state, "files_to_process", session_id=state["call"]["session_id"], asked=asked)
    return {"files": result.get("files", []), "outcomes": [result]}


@stops_at("resume")
def after_files(state: Run):
    """Route to "dossier_import" when there are new files and the client's message asks for an import folder
    (« dossier d'import »); else route as each_file does."""
    if not failed(state) and state["files"] and folder_mode(state["call"].get("client_message") or "")[0]:
        return "dossier_import"
    return each_file(state)


async def import_folder(state: Run):
    """Graph node: make the new files into one Experio import folder (create_import_folder) and set
    "folder_invoices" to the drafts it made."""
    message = state["call"].get("client_message") or ""
    made = await run_step(
        state,
        "create_import_folder",
        session_id=state["call"]["session_id"],
        dossier_id=state["dossier_id"],
        client_message=message,
    )
    return {"files": [], "folder_invoices": made.get("invoices", []), "outcomes": [made]}


@stops_at("resume")
def each_file(state: Run):
    """Route: one document run per import folder draft (from wait_until_read), else one per new file; "brouillons"
    when there is no new file; "resume" when the files could not be listed."""
    if failed(state):
        return "resume"  # the files could not be listed: recorded, nothing more
    if state.get("folder_invoices"):  # an import folder's drafts, each from Experio's reading on, transit last
        call = with_dossier(state)
        return [
            Send(
                "fichier",
                {"call": call, "run_id": state["run_id"], "entry": "wait_until_read", "invoice_id": i, "outcomes": []},
            )
            for i in state["folder_invoices"]
        ]
    if not state["files"]:
        return "brouillons"
    return [
        Send("fichier", {"call": with_dossier(state), "run_id": state["run_id"], "file_id": file_id, "outcomes": []})
        for file_id in state["files"]
    ]


async def waiting(state: Run):
    """Graph node: list the drafts and statements to take up again (waiting_drafts), leaving out skip_invoices.

    A client's message (not a follow-up, mode resume) also takes up drafts a step stopped and, with comptabiliser
    "oui", the dossier's drafts held back in any conversation."""
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
        client_message=call.get("client_message") or "",
        compte=call.get("compte") or "",
    )
    skipped = set(call.get("skip_invoices") or [])  # taken up by their own branch of this run (resume_at)
    invoices = [invoice_id for invoice_id in result.get("invoices", []) if invoice_id not in skipped]
    billed = [invoice_id for invoice_id in result.get("facturation", []) if invoice_id not in skipped]
    corrections = result.get("corrections") or []
    statements = result.get("statements") or []
    found = {"invoices": invoices, "facturation": billed, "corrections": corrections, "statements": statements}
    return {**found, "outcomes": [result]}


# What a client gives « with their invoices »: for the invoices of this message, never a draft taken up again.
MESSAGE_INSTRUCTIONS = ("compte", "tva_non_recuperable", "retenue", "repartir", "mode_paiement", "exercice")


def each_draft(state: Run):
    """Route the drafts to take up again, each as a document run without this message's instructions (they are for
    the invoices sent with it): a draft from wait_until_read; a draft the client gave an account to (corrections)
    from assign_account, that account to confirm; a Facturation draft from configure_cabinet; a statement from its
    pointing. "resume" when there is nothing to take up."""
    corrections = {int(invoice_id): account for invoice_id, account in state.get("corrections") or []}
    statements = state.get("statements") or []
    if not state.get("invoices") and not state.get("facturation") and not corrections and not statements:
        return "resume"
    resumed = {**with_dossier(state), **dict.fromkeys(MESSAGE_INSTRUCTIONS, "")}

    def sent(invoice_id, call, entry):
        return Send(
            "fichier",
            {"call": call, "run_id": state["run_id"], "entry": entry, "invoice_id": invoice_id, "outcomes": []},
        )

    sends = [sent(i, resumed, "wait_until_read") for i in state.get("invoices", []) if i not in corrections]
    sends += [
        sent(i, {**resumed, "compte": account, "confirmer": True}, "assign_account")
        for i, account in corrections.items()
    ]
    # Experio's own invoicing made these (Facturation): nothing to read nor check, their settings then posting.
    sends += [sent(i, resumed, "configure_cabinet") for i in state.get("facturation", [])]
    # A statement waiting on Experio's reading, or to post now the client asks: from its pointing.
    sends += [
        Send(
            "fichier", {"call": resumed, "run_id": state["run_id"], "entry": "statement", "file_id": f, "outcomes": []}
        )
        for f in statements
    ]
    return sends


async def answer(state: Run):
    """Graph node: pass the client's answer on to the question waiting (answer_pending)."""
    call = state["call"]
    result = await run_step(
        state,
        "answer_pending",
        session_id=call["session_id"],
        answer=call.get("answer") or call.get("client_message") or "",
        decision=str(call.get("decision") or ""),
        about=str(call.get("about") or ""),
        corrections=call.get("corrections") or [],
    )
    return {"outcomes": [result]}


def resume_at(state: Run):
    """Route after the answer: the answered invoice from its next step, the waiting drafts ("brouillons"), a statement
    or a file to identify again, or "resume" when nothing follows.

    Every invoice the client gave an account with the answer (corrections) also goes back to assign_account, even
    when the answer itself settled nothing; an invoice other than the one answered about gets its account to
    confirm, never applied as is."""
    last = state["outcomes"][-1]
    following = last.get("next_step")
    corrections = {int(invoice_id): account for invoice_id, account in last.get("corrections") or []}
    answered, call = last.get("invoice_id"), with_dossier(state)

    def sent(invoice_id, entry):
        # Another invoice than the one answered about: its account is asked to be confirmed, never applied as is.
        given = {"compte": corrections.get(invoice_id, ""), "confirmer": invoice_id != answered}
        payload = {"call": {**call, **given}, "run_id": state["run_id"]}
        return Send("fichier", {**payload, "entry": entry, "invoice_id": invoice_id, "outcomes": []})

    fixes = [sent(invoice_id, "assign_account") for invoice_id in corrections if invoice_id != answered]
    if last.get("status") != "ok" or not following:
        if answered in corrections:
            fixes.append(sent(answered, "assign_account"))
        return fixes or "resume"
    if following == "waiting_drafts":
        if answered in corrections:
            fixes.append(sent(answered, "assign_account"))
        if not fixes:
            return "brouillons"
        # The drafts taken up again leave the corrected ones to their own branch: never one invoice twice in a run.
        skipped = {**state, "call": {**state["call"], "skip_invoices": sorted(corrections)}}
        return [*fixes, Send("brouillons", skipped)]
    if following == "settle_statement":  # a TPE statement given its sales' client: its pointing goes on
        payload = {"call": call, "run_id": state["run_id"], "entry": "statement", "file_id": last["file_id"]}
        return Send("fichier", {**payload, "outcomes": []})
    if following == "identify_document":  # the client said what the file is: it starts over from there
        call = {**state["call"], "dossier_id": last["dossier_id"], "invoice_type": last.get("invoice_type") or ""}
        return Send("fichier", {"call": call, "run_id": state["run_id"], "file_id": last["file_id"], "outcomes": []})
    # The invoice answered about goes on from its next step; back to its account step when the client gave it one.
    if answered in corrections and following in INVOICE_CHAIN and position(following) > position("assign_account"):
        following = "assign_account"
    return [sent(answered, following), *fixes]


run = StateGraph(Run)
run.add_node("debut", started(INPUTS))
run.add_node("aiguillage", aiguillage)
run.add_node("dossier", dossier_found)
run.add_node("expediteur", sender)
run.add_node("fichiers", files)
run.add_node("dossier_import", import_folder)
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
run.add_conditional_edges("fichiers", after_files, ["dossier_import", "fichier", "brouillons", "resume"])
run.add_conditional_edges("dossier_import", each_file, ["fichier", "brouillons", "resume"])
run.add_conditional_edges("brouillons", each_draft, ["fichier", "resume"])
run.add_conditional_edges("reponse", resume_at, ["fichier", "brouillons", "resume"])
run.add_edge("fichier", "resume")
run.add_edge("resume", END)
# One task at a time: two invoices of one new supplier never both create
# it (link_tiers), and the second of two identical invoices sees the first (check_duplicate).
graph = run.compile(name="Tarik - Traitement").with_config(max_concurrency=1)
