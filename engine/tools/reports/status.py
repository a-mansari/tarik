"""The dossier status report: where a dossier stands in Experio, as a document to keep and to send (Tarik_rapport,
kind "statut").

The figures are Experio's own, read as dossier_progress reads them (dossiers/progress.py: its avancement, the exercice's
invoices, bank and TPE statements by status, the bank operations in suspense, the year's declarations). The report
sets them in tables and adds what the numbers show by rules of Tarik's own, never a model's: what is still a draft,
what is unpaid, what waits in suspense, a declaration left in draft. A rule says only what the counts say, and the
report says it is an automatic reading, to be checked."""

from datetime import UTC, datetime

from engine.tools.core.app import ToolError, tool
from engine.tools.core.words import counted
from engine.tools.dossiers.people import check_sender
from engine.tools.dossiers.progress import (
    BOOKED,
    DECLARATION_STATES,
    PAYMENT_STATES,
    STATEMENTS,
    STATES,
    french_date,
    status_of,
)
from engine.tools.reports.document import parse
from engine.tools.reports.files import formats_of, room, written

SIDES = (("purchase", "d'achat", "Achats"), ("sale", "de vente", "Ventes"))


def fr(n):
    """Return the whole number ``n`` as a French figure: « 18 895 », a no-break space between thousands."""
    return f"{n:,}".replace(",", "\u00a0")


def many(n, one, several):
    """Return « 1 facture » or « 3 factures » from ``n`` and the two forms with {}."""
    return counted(n, one, several)


def column(words, feminine):
    """Return a state's column header from its French ``words`` (masculine, feminine), in the plural as by_state says
    it: « En brouillon », « Comptabilisées », « Payées en partie »."""
    text = words[feminine]
    first, _, rest = text.partition(" ")
    plural = text if first == "en" else f"{first}s {rest}".strip()  # « en brouillon » never takes an s
    return plural[:1].upper() + plural[1:]


def invoice_table(found):
    """Return the table of the exercice's invoices by side, status and payment: a column for each status seen."""
    states = [state for state in STATES if any(found["states"].get((side, state)) for side, *_ in SIDES)]
    pays = [state for state in PAYMENT_STATES if any(found["payments"].get((side, state)) for side, *_ in SIDES)]
    headers = ["Factures", "Total", *[column(STATES[s], True) for s in states]]
    headers += [f"dont {column(PAYMENT_STATES[p], True).lower()}" for p in pays]  # among the posted ones
    rows = []
    for side, _, label in SIDES:
        mine = {state: n for (kind, state), n in found["states"].items() if kind == side}
        paid = {state: n for (kind, state), n in found["payments"].items() if kind == side}
        counts = [sum(mine.values()), *[mine.get(s, 0) for s in states], *[paid.get(p, 0) for p in pays]]
        rows.append([label, *map(fr, counts)])
    return {
        "type": "table",
        "caption": "Factures de l'exercice (les paiements : parmi les comptabilisées, hors exportées)",
        "headers": headers,
        "rows": rows,
    }


def statement_table(found):
    """Return the table of the exercice's bank and TPE statements and their operations."""
    rows = []
    for model, word in STATEMENTS:
        statements, operations = found["statements"][model]
        rows.append(
            [
                f"Relevés {word}",
                fr(sum(statements.values())),
                fr(sum(statements.get(s, 0) for s in BOOKED)),
                fr(sum(operations.values())),
                fr(sum(operations.get(s, 0) for s in BOOKED)),
            ]
        )
    headers = ["Type", "Relevés", "dont comptabilisés", "Opérations", "dont comptabilisées"]
    return {"type": "table", "caption": "Banque de l'exercice", "headers": headers, "rows": rows}


def declaration_table(found):
    """Return the table of the year's declarations by period and status, None when the dossier has none."""
    rows = []
    for name, periods in found["declarations"]:
        for declaration in periods.get(found["id"], []):
            period = (declaration["period_id"] or [0, str(found["year"])])[1]
            rows.append([name, period, DECLARATION_STATES.get(declaration["state"], "état inconnu")])
    if not rows:
        return None
    return {
        "type": "table",
        "caption": "Périodes de l'année",
        "headers": ["Déclaration", "Période", "État"],
        "rows": rows,
    }


def attention(found):
    """Return what the counts show, one sentence each, by Tarik's own rules (see the module's words)."""
    if not found["exercice"]:
        return ["Aucun exercice dans Experio pour ce dossier : rien à vérifier tant qu'il n'est pas ouvert."]
    out = []
    progress = found["progress"][0]
    if progress is not None and progress < 100:
        out.append(f"L'avancement est de {progress} % : des pièces ou des opérations restent à comptabiliser.")
    for side, word, _ in SIDES:
        mine = {state: n for (kind, state), n in found["states"].items() if kind == side}
        for state, said in (
            ("draft", "en brouillon, à vérifier"),
            ("pending", "en cours de traitement par Experio (à signaler si cela dure)"),
        ):
            if mine.get(state):
                out.append(f"{many(mine[state], '{} facture ' + word, '{} factures ' + word)} {said}.")
        paid = {state: n for (kind, state), n in found["payments"].items() if kind == side}
        posted, unpaid = sum(paid.values()), paid.get("unpaid", 0)
        if unpaid:
            share = round(100 * unpaid / posted)
            # posted ones only (progress.invoice_counts): the exported ones are not in these counts
            out.append(
                f"Factures {word} comptabilisées (hors exportées) impayées : {fr(unpaid)} sur {fr(posted)} ({share} %)."
            )
    for model, word in STATEMENTS:
        drafts = found["statements"][model][0].get("draft", 0)
        if drafts:
            out.append(f"{many(drafts, '{} relevé ' + word, '{} relevés ' + word)} en brouillon.")
    if found["suspense"]:
        left = many(found["suspense"], "{} opération bancaire", "{} opérations bancaires")
        out.append(f"{left} comptabilisées non lettrées (en attente ou lettrées en partie).")
    for name, periods in found["declarations"]:
        drafts = [
            (d["period_id"] or [0, str(found["year"])])[1]
            for d in periods.get(found["id"], [])
            if d["state"] == "draft"
        ]
        if drafts:
            out.append(f"{name} : {', '.join(drafts)} en brouillon.")
    return out or ["Rien à signaler dans ces chiffres."]


def status_document(found, today=None):
    """Return the Document of a dossier's status from ``found`` (progress.status_of's entry, with its ``id``), dated
    ``today`` (a date; now when not given)."""
    today = today or datetime.now(UTC).date()
    exercice = found["exercice"]
    period = (
        f"{exercice['name']}, du {french_date(exercice['date_start'])} au {french_date(exercice['date_end'])}"
        if exercice
        else "aucun exercice dans Experio"
    )
    progress, pieces_booked, pieces, operations_booked, operations = found["progress"]
    blocks = [
        {"type": "heading", "level": 1, "text": "Avancement"},
        {
            "type": "table",
            "caption": "Chiffres clés (tous exercices)",
            "headers": ["Mesure", "Valeur"],
            "rows": [
                ["Avancement selon Experio", "rien à traiter pour l'instant" if progress is None else f"{progress} %"],
                ["Pièces comptabilisées", f"{fr(pieces_booked)} sur {fr(pieces)}"],
                ["Opérations bancaires et de caisse comptabilisées", f"{fr(operations_booked)} sur {fr(operations)}"],
                ["Opérations bancaires comptabilisées non lettrées", fr(found["suspense"])],
            ],
        },
    ]
    if exercice:
        blocks += [{"type": "heading", "level": 1, "text": "L'exercice"}, invoice_table(found), statement_table(found)]
        declarations = declaration_table(found)
        if declarations:
            blocks += [{"type": "heading", "level": 1, "text": f"Déclarations {found['year']}"}, declarations]
    else:
        blocks.append({"type": "paragraph", "text": "Le dossier n'a aucun exercice dans Experio : rien à détailler."})
    blocks += [
        {"type": "heading", "level": 1, "text": "Points d'attention"},
        {"type": "bullets", "items": attention(found)},
        {
            "type": "note",
            "text": f"Chiffres lus dans Experio le {today:%d/%m/%Y}. Les points d'attention sont une lecture "
            "automatique de ces chiffres, à valider.",
        },
    ]
    return parse(f"État du dossier {found['name']}", blocks, period)


REPORTS = {"statut": "L'état d'un dossier : avancement, factures, banque, déclarations, points d'attention."}


@tool
def make_report(session_id: str, kind: str, dossier_id: int, formats: list, year: int = 0, filename: str = "") -> str:
    """Make a report on dossier ``dossier_id`` (``kind``: one of REPORTS; "statut": where it stands in Experio) as
    ``formats`` (docx, xlsx, pdf, md, tex), for the exercice of ``year`` (0: its latest), and keep the files in
    conversation ``session_id``'s outbox (files.written). The conversation's sender must have the dossier, as for any
    work on it (check_sender).

    Returns French text saying which files were made. Raises ToolError for an unknown kind or format, a sender who may
    not read the dossier, an exercice the dossier lacks, Experio not answering, or what files.written refuses."""
    if kind not in REPORTS:
        raise ToolError(f"Rapport : un type parmi {', '.join(REPORTS)} (pas « {kind} »).")
    wanted = formats_of(formats)  # before any Experio read: a wrong format, or an outbox already full, costs nothing
    if not session_id:
        raise ToolError("Aucune conversation où garder le rapport.")
    if year and not 1990 <= year <= 2100:
        raise ToolError(f"Exercice : une année entre 1990 et 2100 (pas {year}).")
    room(session_id, len(wanted))
    allowed = check_sender(session_id, dossier_id)
    if allowed["status"] == "stop":
        raise ToolError(allowed["message"])
    found = {**status_of(dossier_id, year), "id": dossier_id}
    return written(session_id, status_document(found), formats, filename)
