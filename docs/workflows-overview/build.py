"""Build the Tarik workflows overview for the project team: index.html (from overview.html, written by hand),
one page per workflow (emails, traitement, dossier .html), each with its map drawn from
workflows/tarik-*.json (n8n's node positions, workflows/layout.json) and every node explained below, and
guide.html, the developers' repository guide rendered from docs/repository-guide.md. Then, with
Google Chrome or Chromium installed, a PDF of each page, and with pdfunite tarik-workflows.pdf, all of them in one.

Run on the host after a workflow changes: python3 docs/workflows-overview/build.py
It fails when a node has no explanation here: add one to its page below."""

# ruff: noqa: E501 -- its rows are prose for the reader, one string each: wrapping them helps nobody.

import html
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parents[1]
OUT = HERE
OVERVIEW = (HERE / "overview.html").read_text()
BASE_CSS = re.search(r"<style>(.*?)</style>", OVERVIEW, re.S).group(1)
FONTS = "\n".join(line for line in OVERVIEW.splitlines() if line.startswith("<link"))

EXTRA_CSS = """
.crumb { font-family: var(--mono); font-size: 12px; letter-spacing: .06em; text-transform: uppercase; }
.crumb a { color: var(--accent); text-decoration: none; }
.crumb a:hover, .crumb a:focus-visible { text-decoration: underline; }
.facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.fact { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 12px 14px; min-width: 0; }
.fact b { display: block; font-family: var(--mono); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 500; margin-bottom: 4px; }
.map { overflow-x: auto; background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 12px; }
.map svg { display: block; width: 100%; height: auto; }
.map .edge { fill: none; stroke: var(--rule); stroke-width: 2; }
.map .box { stroke-width: 2; }
.map .label { fill: var(--ink); font-family: var(--body); font-size: 20px; }
.k-trigger { fill: var(--accent-soft); stroke: var(--accent); }
.k-step { fill: var(--sheet); stroke: var(--accent); }
.k-decision { fill: var(--warn-soft); stroke: var(--warn); }
.k-helper { fill: var(--paper); stroke: var(--muted); }
.k-sub { fill: var(--accent-soft); stroke: var(--ink); }
.k-record { fill: var(--ink); stroke: var(--ink); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--muted); margin-top: 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 14px; height: 14px; border-radius: 4px; border: 2px solid; display: inline-block; }
.legend .k-trigger { background: var(--accent-soft); border-color: var(--accent); }
.legend .k-step { background: var(--sheet); border-color: var(--accent); }
.legend .k-decision { background: var(--warn-soft); border-color: var(--warn); }
.legend .k-helper { background: var(--paper); border-color: var(--muted); }
.legend .k-sub { background: var(--accent-soft); border-color: var(--ink); }
.legend .k-record { background: var(--ink); border-color: var(--ink); }
.phase { display: grid; gap: 10px; }
.phase > p { margin: 0; color: var(--muted); }
.steps { display: grid; gap: 0; border-top: 1px solid var(--rule); }
.step { display: grid; grid-template-columns: minmax(0, 300px) minmax(0, 1fr); gap: 4px 20px; padding: 10px 0; border-bottom: 1px solid var(--rule); break-inside: avoid; }
.step code { font-family: var(--mono); font-size: 13px; color: var(--ink); background: var(--accent-soft); padding: 2px 6px; border-radius: 4px; justify-self: start; overflow-wrap: anywhere; }
.step p { margin: 0; }
.chain { counter-reset: s; }
.chain .step code::before { counter-increment: s; content: counter(s) ". "; color: var(--accent); }
@media screen and (max-width: 760px) {
  .facts { grid-template-columns: 1fr; }
  .step { grid-template-columns: 1fr; }
}
@page { size: A4 landscape; margin: 12mm; }
.print-only { display: none; }
@media print {
  body { font-size: 12px; background: #fff; }
  .page { max-width: none; padding: 0; gap: 18px; }
  .print-only { display: block; }
  .facts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .step { grid-template-columns: minmax(0, 300px) minmax(0, 1fr); }
  .mapwrap { break-before: page; break-inside: avoid; }
  .map { overflow: visible; }
  .map svg { min-width: 0 !important; }
  .phase { break-inside: auto; }
  .card, .fact, .limits { break-inside: avoid; }
  a.more { display: none; }
}
"""
PRINT_OVERVIEW = """
@page { size: A4; margin: 14mm; }
@media print {
  body { font-size: 12px; background: #fff; }
  .page { max-width: none; padding: 0; gap: 18px; }
  .flow { overflow: visible; }
  .flow-inner { min-width: 0; }
  .card, .limits { break-inside: avoid; }
  .grid, .foot { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  a.more { display: none; }
}
"""

KIND = {
    "executeWorkflowTrigger": "trigger",
    "webhook": "trigger",
    "mcpClient": "step",
    "switch": "decision",
    "code": "helper",
    "executeWorkflow": "sub",
}
KIND_NAMES = {
    "trigger": "Start",
    "step": "Tarik tool (one Experio action)",
    "decision": "Decision",
    "helper": "Sorting (no Experio action)",
    "sub": "Runs another workflow",
    "record": "Record",
}


def kind(node):
    if node["name"] == "Enregistrer":
        return "record"
    return KIND.get(node["type"].split(".")[-1], "helper")


def wrap(text, width=20):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line]


def svg_map(workflow):
    """The workflow drawn as n8n's canvas shows it: each node at its position, named, joined to where it leads."""
    nodes = {n["name"]: n for n in workflow["nodes"]}
    size, pad = 80, 60
    xs = [n["position"][0] for n in nodes.values()]
    ys = [n["position"][1] for n in nodes.values()]
    x0, y0 = min(xs) - pad, min(ys) - pad
    width, height = max(xs) - x0 + size + pad + 60, max(ys) - y0 + size + pad + 70
    edges, boxes = [], []
    for source, outputs in workflow["connections"].items():
        sx, sy = nodes[source]["position"]
        for output in outputs.get("main", []):
            for link in output:
                if link["node"] == "Enregistrer":
                    continue  # every step records there: its lines would hide the flow
                tx, ty = nodes[link["node"]]["position"]
                a, b = (sx + size - x0, sy + size / 2 - y0), (tx - x0, ty + size / 2 - y0)
                bend = max(40, abs(b[0] - a[0]) / 2)
                edges.append(
                    f'<path class="edge" d="M{a[0]:.0f},{a[1]:.0f} C{a[0] + bend:.0f},{a[1]:.0f} '
                    f'{b[0] - bend:.0f},{b[1]:.0f} {b[0]:.0f},{b[1]:.0f}"/>'
                )
    for name, node in nodes.items():
        x, y = node["position"][0] - x0, node["position"][1] - y0
        lines = "".join(
            f'<tspan x="{x + size / 2:.0f}" dy="{0 if i == 0 else 23}">{html.escape(line)}</tspan>'
            for i, line in enumerate(wrap(name))
        )
        boxes.append(
            f'<g><title>{html.escape(name)}</title><rect class="box k-{kind(node)}" x="{x:.0f}" y="{y:.0f}" '
            f'width="{size}" height="{size}" rx="14"/>'
            f'<text class="label" text-anchor="middle" x="{x + size / 2:.0f}" y="{y + size + 26:.0f}">{lines}</text></g>'
        )
    legend = "".join(
        f'<span><i class="k-{k}"></i>{label}</span>'
        for k, label in KIND_NAMES.items()
        if any(kind(n) == k for n in nodes.values())
    )
    min_width = round(width * 0.55)  # readable on screen (labels about 11 px); the page scrolls sideways
    return (
        f'<div class="map"><svg viewBox="0 0 {width:.0f} {height:.0f}" style="min-width:{min_width}px" role="img" '
        f'aria-label="Map of {html.escape(workflow["name"])} as laid out in n8n">'
        + "".join(edges)
        + "".join(boxes)
        + f'</svg></div><div class="legend">{legend}</div>'
        + (
            '<p class="legend">Every step also records its outcome in Enregistrer; those lines are left out.</p>'
            if "Enregistrer" in nodes
            else ""
        )
        + (
            '<p class="legend print-only">Scaled to fit the page: the HTML version shows this map full size.</p>'
            if len(nodes) > 30
            else ""
        )
    )


def phase(title, intro, rows, chain=False):
    items = "".join(f'<div class="step"><code>{html.escape(name)}</code><p>{text}</p></div>' for name, text in rows)
    steps = f'<div class="steps{" chain" if chain else ""}">{items}</div>'
    return f'<section class="phase"><h2>{title}</h2>{f"<p>{intro}</p>" if intro else ""}{steps}</section>'


def page(slug, title, lede, facts, sections, workflow):
    names = {n["name"] for n in workflow["nodes"]}
    explained = set(re.findall(r"<code>(.*?)</code>", "".join(sections)))
    missing = {
        n for n in names if html.escape(n) not in explained and not n.startswith(("Suite apres ", "Hors delai ("))
    }
    assert not missing, f"{slug}: nodes not explained: {sorted(missing)}"
    fact_html = "".join(f'<div class="fact"><b>{k}</b>{v}</div>' for k, v in facts)
    body = f"""<title>{html.escape(title)}</title>
{FONTS}
<style>{BASE_CSS}{EXTRA_CSS}</style>
<div class="page">
  <header>
    <div class="crumb"><a href="index.html">← Tarik workflows overview</a></div>
    <h1>{html.escape(title)}</h1>
    <p class="lede">{lede}</p>
  </header>
  <div class="facts">{fact_html}</div>
  <section class="phase mapwrap"><h2>The workflow in n8n</h2>
    <p>Each box is a node, named exactly as in n8n's editor and placed where it sits there. Lines show where each node leads.</p>
    {svg_map(workflow)}
  </section>
  {"".join(sections)}
  <div class="stamp">State as of {date.today():%d %B %Y} · node names as in n8n's editor</div>
</div>
"""
    return body


def full(body, lang="en"):
    return f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n</head>\n<body>\n{body}</body>\n</html>\n'


def load(name):
    return json.loads((REPO / "workflows" / f"tarik-{name}.json").read_text())


def ending(recorded, summary):
    """A run's last rows: Enregistrer (``recorded``), then, once every step has ended, Résumé (``summary``)."""
    return [
        ("Enregistrer", recorded),
        (
            "Fin des etapes",
            "Waits for every step to end (its second input, from Jamais execute, never comes), then lets Résumé run.",
        ),
        ("Jamais execute", "Never runs: it only keeps Fin des etapes waiting until the end."),
        ("Résumé", f"After Fin des etapes: {summary}"),
    ]


PATTERN_ROWS = [
    (
        "Suite apres <step>",
        "After each step, a decision. The step went through: on to the next step. It asked the client a question, stopped the invoice (a duplicate, a missing setting) or failed: the outcome is recorded and this invoice goes no further in this run.",
    ),
    (
        "Hors delai (<step>)",
        "The run has used its 240 seconds: the invoice is recorded as waiting at this step instead of starting the next one. The client's next message, or the email follow-up, takes it up again from Experio's reading.",
    ),
]

PAGES = {}

PAGES["emails"] = page(
    "emails",
    "Tarik - Emails",
    "Answers each firm's Tarik mailbox. Woken by the mail watcher whenever an email thread waits for an answer, it hands each thread to the firm's AI agent and sends the reply in the same thread. When work was left waiting, it sends a follow-up email later, without the agent.",
    [
        ("Started by", "The mail watcher, through a protected webhook, naming the firm."),
        ("Works on", "Every email thread of that firm waiting for an answer, one at a time."),
        ("Ends with", "A reply or a follow-up in the thread, and the thread's new state."),
    ],
    [
        phase(
            "Picking up the threads",
            "",
            [
                (
                    "Nouvel email",
                    "The webhook the mail watcher calls when a thread waits: a new email arrived, or a follow-up is ready. It only answers calls carrying the shared secret, and it names the firm.",
                ),
                (
                    "Fils en attente (claim_threads)",
                    "Takes the firm's threads waiting for an answer and locks them for this run, so two runs never answer the same thread.",
                ),
                ("Un element par fil", "Splits them up: each thread goes through the rest on its own."),
                (
                    "Suite ?",
                    "A thread with new emails from the client goes to the agent. A follow-up (nothing new from the client, work left waiting) skips the agent.",
                ),
            ],
        ),
        phase(
            "Answering with the agent",
            "",
            [
                (
                    "Accuse de reception (acknowledge_thread)",
                    "A new conversation (its first email, not a reply in an earlier thread) gets an instant reply in the thread: “Votre message est bien reçu. Je le traite et vous réponds dans ce fil, en général d'ici quelques minutes.” Once per thread, never for a reply or a follow-up. If it cannot be sent, the agent answers all the same.",
                ),
                (
                    "Tarik",
                    "Asks the firm's AI agent to answer the thread's new emails, in the thread's own conversation, with its attachments. The agent calls Tarik - Traitement, Tarik - Dossier or Tarik - Modification. Waits up to 11 minutes for the answer.",
                ),
                (
                    "Refus de droits ? (thread_refusal)",
                    "Checks whether the work was refused because the sender is not allowed on that client file.",
                ),
                (
                    "Refuse ?",
                    "Allowed: the answer is sent. Refused: the sender gets nothing and the firm's manager is told.",
                ),
                (
                    "Reponse (send_email)",
                    "Sends the agent's answer as a reply in the thread, only to approved addresses.",
                ),
                (
                    "Manager prevenu (notify_manager)",
                    "Emails the firm's manager that this sender wrote and was refused, at most once a day per sender.",
                ),
            ],
        ),
        phase(
            "Following up",
            "Only for a thread whose work was left waiting: invoices Experio was still reading, or a new client file still onboarding. No agent, and no time limit.",
            [
                (
                    "Suite du traitement",
                    "Runs Tarik - Traitement in resume mode: the conversation's invoices left waiting are taken up again.",
                ),
                (
                    "Suite de l'integration",
                    "Runs Tarik - Dossier in resume mode: the conversation's client files left waiting are finished, and their state is read.",
                ),
                (
                    "Suite envoyee (send_email)",
                    "Sends both outcomes in one email in the same thread, starting “Suite du traitement de vos documents :”. A part with nothing to do is left out.",
                ),
            ],
        ),
        phase(
            "Closing the thread",
            "",
            [
                (
                    "Fil traite (finish_thread)",
                    "Marks the emails answered and sets the thread: done, waiting for the client's answer to a question, or followed up while work waits (30 minutes at most). When something failed (the reply could not be sent, the agent did not answer), the thread is set aside with the reason. Retried 3 times, so a reply is never sent twice.",
                ),
            ],
        ),
    ],
    load("emails"),
)

PAGES["traitement"] = page(
    "traitement",
    "Tarik - Traitement",
    "Turns the documents a client sent into entries in Experio: bank statements imported, invoices drafted, checked, completed and posted, one fixed step after another. When a step needs the client, it asks one question and waits for the answer.",
    [
        (
            "Started by",
            "The AI agent (Experio_traiter_fichiers to process files, Experio_repondre for an answer), or Tarik - Emails following up.",
        ),
        (
            "Time budget",
            "240 seconds, under the 5 minutes n8n allows. What is left waits for the next run. A follow-up has no budget.",
        ),
        ("Ends with", "A summary invoice by invoice, which the agent copies into its answer."),
    ],
    [
        phase(
            "Starting a run",
            "",
            [
                (
                    "Appel",
                    "The call and what comes with it: the mode (files, answer or resume), the conversation, the client file, purchase or sale as the client said, the client's message, the attachments, the firm.",
                ),
                ("Debut", "Notes when the run started, for its time budget."),
                (
                    "Pieces jointes de l'agent",
                    "Saves files attached in n8n's chat. Files sent by email were saved when the email arrived.",
                ),
                ("Fichiers ou reponse ?", "Files to process, the client's answer to a question, or a follow-up."),
            ],
        ),
        phase(
            "Files: from document to draft",
            "",
            [
                (
                    "Expediteur (check_sender)",
                    "The email's sender must be one of the firm's staff, or of Experio's own team, with this client file assigned. Otherwise the run stops there.",
                ),
                ("Fichiers du message (files_to_process)", "The conversation's files not processed yet."),
                (
                    "Nouveaux fichiers ?",
                    "None new (“relancez le traitement”): the invoices left waiting are taken up instead.",
                ),
                ("Un element par fichier", "One item per file."),
                ("Identification (identify_document)", "Invoice or bank statement?"),
                (
                    "Releve bancaire (import_bank_statement)",
                    "Imports a bank statement into the client file's bank journal. With several bank accounts, asks the client which one.",
                ),
                (
                    "Decoupage (split_invoices)",
                    "Finds each invoice inside a PDF. Needs purchase or sale from the client; otherwise it asks, and nothing is created.",
                ),
                ("Une facture par fichier", "One item per invoice found."),
                (
                    "Brouillon (create_draft_invoice)",
                    "Creates the draft invoice in Experio, never twice for the same file.",
                ),
            ],
        ),
        phase(
            "Each invoice: the fixed sequence",
            "Every invoice goes through these steps in this order. Each one either goes through, asks the client one question, or stops the invoice.",
            [
                (
                    "Lecture Experio (wait_until_read)",
                    "Waits for Experio's own reading (OCR) of the draft: 3 minutes at most for all the run's drafts together.",
                ),
                (
                    "Verification (check_invoice)",
                    "Compares what Experio captured with what the scan shows, and lists every difference. Changes nothing.",
                ),
                (
                    "Correction (correct_invoice)",
                    "Writes what the scan shows over what Experio captured differently: amounts, number, date, credit note, several VAT rates, currency, payment method.",
                ),
                (
                    "Classement (classify_invoice)",
                    "Purchase, sale or in fact a bank statement. A scan that contradicts what the client said stops the invoice.",
                ),
                (
                    "Tiers (link_tiers)",
                    "Sets the supplier or customer Experio detected, with the account it needs. When none was detected, asks the client, suggesting close names.",
                ),
                (
                    "Doublon (check_duplicate)",
                    "Already recorded in this client file (same number, supplier or customer, date)? A duplicate stops here.",
                ),
                (
                    "Compte (assign_account)",
                    "Sets the account: the one Experio remembers for this supplier, else the one matching what is billed (e.g. 6125). When none fits, asks the client.",
                ),
                (
                    "TVA (settle_tva)",
                    "Recoverable or not, the firm's prorata, the tax on each line. Asks when the VAT might not be recoverable.",
                ),
                (
                    "Journal (check_journal)",
                    "Puts the invoice in the journal Experio's rules give it, or names the missing setting.",
                ),
                (
                    "Retenue a la source (settle_withholding)",
                    "Withholding tax on a purchase: the supplier's known rule, else a question.",
                ),
                (
                    "Constatee d'avance (settle_prepaid)",
                    "An invoice covering several months: asks whether to spread it (charges or produits constatés d'avance).",
                ),
                (
                    "Exercice (settle_exercice)",
                    "Books the invoice in an open fiscal year. When none covers its date, asks the client.",
                ),
                (
                    "Parametrage (configure_cabinet)",
                    "When Experio's own check says a setting is missing, fills what its screens would: the taxes the chart of accounts calls for, then Configuration's automatic detection of every empty setting from the chart and the journals. What still blocks is for the firm: an account or a journal the file lacks.",
                ),
                (
                    "Comptabilisation (post_invoice)",
                    "Posts the invoice when Experio's own check finds nothing blocking; otherwise lists what blocks it.",
                ),
            ],
            chain=True,
        ),
        phase("Between the steps", "Two kinds of nodes repeat after every step of the sequence:", PATTERN_ROWS),
        phase(
            "The client's answer",
            "",
            [
                (
                    "Reponse du client (answer_pending)",
                    "Applies the client's answer, in their own words, with what the agent read in it (oui or non, the supplier chosen among those proposed), to the question this conversation waits on: a supplier, an account, VAT, withholding, prepaid, fiscal year, or a statement's bank account. An unclear oui or non is asked again. Checks the sender first.",
                ),
                (
                    "Reprendre ou ?",
                    "Resumes the invoice at the step after the answered one (Doublon, TVA, Journal, Retenue a la source, Constatee d'avance, Exercice or Comptabilisation). With nothing waiting, takes up the invoices left waiting.",
                ),
            ],
        ),
        phase(
            "Taking up waiting invoices",
            "",
            [
                (
                    "Brouillons en attente (waiting_drafts)",
                    "The conversation's drafts left waiting: Experio was still reading them, a step failed, or a run was cut short. Only those still drafts in Experio.",
                ),
                (
                    "Une facture par brouillon",
                    "Each one goes back to Lecture Experio, and every later step runs again.",
                ),
            ],
        ),
        phase(
            "Recording and answering",
            "",
            [
                *ending(
                    "Records every step's outcome for every invoice. A question becomes the invoice's pending question, which the client's answer settles.",
                    "the run's summary, invoice by invoice and step by step. It is the workflow's answer, which the agent copies.",
                ),
            ],
        ),
    ],
    load("traitement"),
)

PAGES["dossier"] = page(
    "dossier",
    "Tarik - Dossier",
    "Opens a new client file (dossier) in Experio, optionally copies another file's setup, imports the client's accounting history (FEC or general ledger), finishes the onboarding and reports where the file stands. Also loads a FEC into an existing file, or (update mode) changes an existing file's details: name, type, ICE, IF, RC, patente, T.S.C, capital, CNSS, address.",
    [
        (
            "Started by",
            "The AI agent (Experio_parametrer_dossier, Experio_modifier_dossier), or Tarik - Emails following up.",
        ),
        (
            "Time budget",
            "260 seconds. When Experio's wait would not fit, the file is left waiting and the email follow-up finishes it.",
        ),
        ("Ends with", "A summary: creation, import, onboarding and the file's state."),
    ],
    [
        phase(
            "Starting a run",
            "",
            [
                (
                    "Appel",
                    "The call: the conversation, the client file (0 for a new one), its name, ICE, IF, company or person, the file whose setup to copy, FEC or general ledger, the attachments, the firm.",
                ),
                ("Debut", "Notes when the run started, for its time budget."),
                ("Pieces jointes de l'agent", "Saves files attached in n8n's chat."),
                (
                    "Nouveau dossier ?",
                    "A new client file, an existing one, a change to one, a person to add or an archiving, a list of files to create, or a follow-up.",
                ),
            ],
        ),
        phase(
            "A new client file",
            "",
            [
                (
                    "Creation (create_dossier)",
                    "Creates the file as Experio's Folders screen does. Experio checks the name is free and the ICE and IF against the official register. A very similar name, or skipping the register check, is confirmed with the client first.",
                ),
                (
                    "Suite apres create_dossier",
                    "Created: on to the next step. A question or a refusal: recorded, and the run stops there.",
                ),
                ("Modele ?", "Copy another file's setup only if the client asked for it."),
                (
                    "Configuration (copy_configuration)",
                    "Copies that file's accounts, taxes, journals, bank operations and settings, before any import.",
                ),
                ("Suite apres copy_configuration", "Copied: on to the import. Otherwise recorded, and the run stops."),
            ],
        ),
        phase(
            "An existing client file",
            "",
            [
                (
                    "Expediteur (check_sender)",
                    "The email's sender must be one of the firm's staff, or of Experio's own team, with this client file assigned.",
                ),
                ("Suite apres check_sender", "Allowed: on to the change or the import. Otherwise the run stops."),
            ],
        ),
        phase(
            "Importing the history",
            "",
            [
                ("Dossier cible", "The client file the rest works on: the one given, or the one just created."),
                (
                    "Fichiers du message (files_to_process)",
                    "The conversation's FEC and general ledger files not imported yet.",
                ),
                ("Un element par fichier", "One item per file."),
                (
                    "Ecritures (import_entries)",
                    "Imports the file through Experio's own import screen, after Experio's test of it: entries (FEC or general ledger: Experio creates the missing accounts, journals, suppliers, customers and fiscal years), or a setup file (chart of accounts, journals, suppliers, customers). A file nothing tells apart is asked about. A refused file is reported; the client sends a corrected one.",
                ),
            ],
        ),
        phase(
            "Changing a client file, giving someone access, archiving",
            "Only in the update, person and archive modes (Experio_modifier_dossier, Experio_creer_acces, Experio_archiver_dossier).",
            [
                (
                    "Modifier ?",
                    "A change, a person or an archiving: on to it. Otherwise the existing file is set up (imports below).",
                ),
                (
                    "Acces (create_person)",
                    "Creates a staff member or a client contact in Experio (email as login, a profile) and puts them on the client file, after reading the details back and the client's yes. One with that email already is only attached.",
                ),
                (
                    "Archivage (archive_dossier)",
                    "Archives the client file (hidden from the lists, nothing deleted, restorable in Experio), after reading back what it hides and the client's yes.",
                ),
                (
                    "Modification (update_dossier)",
                    "Changes only the fields given, as the client file's Experio form does, and only to values the client wrote in this conversation. An ICE, IF or name another file already has is refused. A new ICE or IF is checked against the official register; skipping that check needs the client's confirmation first.",
                ),
            ],
        ),
        phase(
            "A list of client files",
            "Only in dossiers mode (Experio_importer_dossiers).",
            [
                (
                    "Fichiers de dossiers (files_to_process)",
                    "The spreadsheets attached to the conversation, not handled yet.",
                ),
                ("Un element par liste", "One item per spreadsheet."),
                (
                    "Liste de dossiers (import_dossiers)",
                    "Checks the whole file (columns, types, ICE and IF, duplicates in the file and in Experio): one problem and nothing is created, the client corrects the file. All good: the list is read back and the files are created on the client's yes; those Experio refuses are listed.",
                ),
            ],
        ),
        phase(
            "Finishing the onboarding",
            "",
            [
                ("Imports acceptes ?", "Only when no import was refused."),
                (
                    "Banques (ask_banks)",
                    "Asks which bank each bank journal is at, one journal at a time, from Experio's list of banks: a FEC or a journals file names none. The client's answer goes through the processing workflow's answer step.",
                ),
                (
                    "Integration (finish_setup)",
                    "Completes Experio's onboarding steps so Experio processes the imported entries, opening the current fiscal year if none is open. A file set up from separate files, with no entries imported, waits for them until the client says every file was sent. Experio only takes lines once they are a minute old: when that wait does not fit in the run's time, the file is left waiting for the follow-up.",
                ),
                (
                    "Integration terminee ?",
                    "Finished: read the file's state. Left waiting: its state comes with the follow-up.",
                ),
                (
                    "Etat du dossier (setup_status)",
                    "Where the file stands: imported entries by state, fiscal years, and any setting Experio needs before an invoice can be posted.",
                ),
            ],
        ),
        phase(
            "Following up",
            "Only when Tarik - Emails follows up a thread.",
            [
                (
                    "Integrations en attente (waiting_setups)",
                    "The conversation's client files whose entries were imported and whose onboarding still waits.",
                ),
                ("Un element par dossier", "One item per file."),
                ("Integration reprise (finish_setup)", "Finishes the onboarding, with no time limit."),
                ("Reprise terminee ?", "Finished: read the file's state."),
                ("Etat du dossier repris (setup_status)", "Where the file stands, for the follow-up email."),
            ],
        ),
        phase(
            "Recording and answering",
            "",
            [
                *ending("Records every step's outcome.", "the run's summary, which the agent copies."),
            ],
        ),
    ],
    load("dossier"),
)


# --- The repository guide (docs/repository-guide.md), for developers ---

GUIDE_CSS = """
.guide h2 { margin: 28px 0 8px; }
.guide h3 { margin: 22px 0 6px; font-size: 16px; }
.guide p { margin: 0 0 12px; }
.guide ul, .guide ol { margin: 0 0 12px; padding-left: 24px; }
.guide li { margin: 4px 0; }
.guide code { font-family: var(--mono); font-size: 12.5px; background: var(--accent-soft); padding: 1px 5px; border-radius: 4px; overflow-wrap: anywhere; }
.guide table { width: 100%; border-collapse: collapse; margin: 0 0 16px; font-size: 14px; }
.guide th, .guide td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--rule); }
.guide th { font-family: var(--mono); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 500; }
.guide td:first-child { white-space: nowrap; }
.guide .map svg { max-width: 760px; }
@media print { .guide table, .guide tr { break-inside: avoid; } .guide td:first-child { white-space: normal; } }
"""

INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: f"<code>{html.escape(m.group(1))}</code>"),
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
]


def inline(text):
    """One line of Markdown as HTML: code spans kept verbatim, bold and links."""
    parts = re.split(r"(`[^`]+`)", html.escape(text, quote=False))
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{part[1:-1]}</code>")
        else:
            for pattern, repl in INLINE[1:]:
                part = pattern.sub(repl, part)
            out.append(part)
    return "".join(out)


def markdown_html(text):
    """The guide's Markdown as HTML: headings, paragraphs, bullet and numbered lists, pipe tables; a mermaid
    block becomes the architecture drawing. Enough for docs/repository-guide.md, not a general converter."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            fence = i
            while not lines[i + 1].startswith("```"):
                i += 1
            i += 2
            if lines[fence].startswith("```mermaid"):
                out.append(f'<div class="map">{guide_svg()}</div>')
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(line.lstrip('#').strip())}</h{level}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([cell.strip() for cell in lines[i].strip().strip("|").split(" | ")])
                i += 1
            head, body = rows[0], [row for row in rows[1:] if not set("".join(row)) <= set("- ")]
            cells = lambda row, tag: "".join(f"<{tag}>{inline(cell)}</{tag}>" for cell in row)  # noqa: E731
            out.append(
                f"<table><thead><tr>{cells(head, 'th')}</tr></thead><tbody>"
                + "".join(f"<tr>{cells(row, 'td')}</tr>" for row in body)
                + "</tbody></table>"
            )
        elif re.match(r"\s*(-|\d+\.)\s", line):
            kind = "ol" if re.match(r"\s*\d+\.", line) else "ul"
            items = []
            while i < len(lines) and lines[i].strip():
                if re.match(r"\s*(-|\d+\.)\s", lines[i]):
                    items.append(re.sub(r"^\s*(-|\d+\.)\s", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()  # a wrapped item
                i += 1
            out.append(f"<{kind}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{kind}>")
        elif line.strip():
            paragraph = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "```")):
                paragraph.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(paragraph))}</p>")
        else:
            i += 1
    return "\n".join(out)


def guide_svg():
    """The architecture drawing: how a request reaches Experio (the same picture as the guide's mermaid block)."""
    W, H = 150, 48
    row_a, row_b, row_c, row_d = 96, 200, 304, 408
    c1, c2, c3, c4, mx0, mx1 = 100, 280, 460, 640, 400, 700
    boxes = [  # (cx, cy, width, main, name, sub)
        (c1, row_a, W, False, "Client", "a cabinet's clerk"),
        (c2, row_a, W, False, "Mail watcher", "mcp/watcher.py"),
        (c3, row_a, W, False, "Tarik - Emails", "n8n workflow"),
        (c4, row_a, W, False, "AI agent", "one per cabinet, in n8n"),
        (c3, row_b, W, False, "Tarik - Traitement", "invoices, statements"),
        (c4, row_b, W, False, "Tarik - Dossier", "create, FEC, update"),
        (
            (mx0 + mx1) / 2,
            row_c,
            mx1 - mx0,
            True,
            "tarik-mcp: the Python tools, one step per call",
            "mcp/tarik/ at /c/<cabinet>/mcp",
        ),
        (c2, row_c, W, False, "Postgres", "one schema per cabinet"),
        (c3, row_d, W, False, "Vault", "secrets and settings"),
        (c4, row_d, W, False, "Experio", "Odoo 15, over RPC"),
    ]
    arrows = [
        f"M{c1 + W / 2} {row_a}H{c2 - W / 2}",
        f"M{c2 + W / 2} {row_a}H{c3 - W / 2}",
        f"M{c3 + W / 2} {row_a}H{c4 - W / 2}",
        f"M{c1} {row_a - H / 2}V58H{c4}V{row_a - H / 2}",
        f"M{c4} {row_a + H / 2}V{row_b - H / 2}",
        f"M{c4 - 40} {row_a + H / 2}V{(row_a + row_b) / 2}H{c3}V{row_b - H / 2}",
        f"M{c3} {row_b + H / 2}V{row_c - H / 2}",
        f"M{c4} {row_b + H / 2}V{row_c - H / 2}",
        f"M{c3} {row_c + H / 2}V{row_d - H / 2}",
        f"M{c4} {row_c + H / 2}V{row_d - H / 2}",
    ]
    parts = [
        '<svg viewBox="0 0 760 460" role="img" aria-label="Every write to Experio goes through a workflow step, never through the model" font-size="13" font-family="var(--body)">',
        '<defs><marker id="guide-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--muted)"/></marker></defs>',
        '<text x="24" y="34" font-size="15" font-weight="600" fill="var(--ink)">Every write to Experio goes through a workflow step, never through the model</text>',
        '<g fill="none" stroke="var(--muted)" stroke-width="1.25">',
        *(f'<path d="{d}" marker-end="url(#guide-arrow)"/>' for d in arrows),
        f'<path d="M{c2 + W / 2} {row_c}H{mx0}"/></g>',
    ]
    for cx, cy, width, main, name, sub in boxes:
        stroke, fill = ("var(--accent)", "var(--accent-soft)") if main else ("var(--rule)", "var(--sheet)")
        parts.append(
            f'<rect x="{cx - width / 2}" y="{cy - H / 2}" width="{width}" height="{H}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="{2 if main else 1.25}"/>'
            f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-weight="600" fill="var(--ink)">{html.escape(name)}</text>'
            f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="11.5" fill="var(--muted)">{html.escape(sub)}</text>'
        )
    parts.append(
        f'<text x="{(c1 + c4) / 2}" y="52" text-anchor="middle" font-size="11.5" fill="var(--muted)">chat, straight to the agent</text>'
    )
    parts.append(
        f'<text x="{c4 + 8}" y="{(row_a + row_b) / 2 + 4}" font-size="11.5" fill="var(--muted)">summary back</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def guide_page():
    """docs/repository-guide.md as a page of the overview, in its style."""
    text = (REPO / "docs/repository-guide.md").read_text()
    title, _, rest = text.partition("\n")
    body = markdown_html(rest).replace('href="../README.md"', 'href="https://github.com/a-mansari/tarik#readme"')
    return f"""<title>{html.escape(title.lstrip("# "))}</title>
{FONTS}
<style>{BASE_CSS}{EXTRA_CSS}{GUIDE_CSS}</style>
<div class="page guide">
  <header>
    <div class="crumb"><a href="index.html">← Tarik workflows overview</a></div>
    <h1>{html.escape(title.lstrip("# "))}</h1>
  </header>
  {body}
  <div class="stamp">State as of {date.today():%d %B %Y} · docs/repository-guide.md</div>
</div>
"""


PAGES["guide"] = guide_page()

overview = OVERVIEW.replace("</style>", PRINT_OVERVIEW + "</style>", 1)
(OUT / "index.html").write_text(full(overview))
for slug, body in PAGES.items():
    (OUT / f"{slug}.html").write_text(full(body))
print("written:", ", ".join(["index", *PAGES]))

chrome = shutil.which("google-chrome") or shutil.which("chromium")
if not chrome:
    print("no Google Chrome or Chromium: PDFs not made")
else:
    pdfs = []
    for slug in ["index", *PAGES]:
        pdf = OUT / f"{slug}.pdf"
        page_url = (OUT / f"{slug}.html").resolve().as_uri()
        flags = ["--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000"]
        subprocess.run(
            [chrome, *flags, f"--print-to-pdf={pdf}", page_url], check=True, capture_output=True
        )  # the time budget lets the web fonts load
        pdfs.append(str(pdf))
    merged = shutil.which("pdfunite")
    if merged:
        subprocess.run([merged, *pdfs, str(OUT / "tarik-workflows.pdf")], check=True)
    print("printed:", ", ".join(Path(pdf).name for pdf in pdfs), "+ tarik-workflows.pdf" if merged else "")
