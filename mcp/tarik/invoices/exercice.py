"""The fiscal year (exercice) the invoice is booked in.

Experio links the invoice to the exercice covering its date itself (exercice_id is computed
from invoice_date), books a late invoice with its own action_activate_old_invoice, and opens
an exercice through action_open after checking its rules (follows the previous one, at most
12 months, no overlap). Left here: relinking when the exercice came after the invoice, and the
question when no open exercice covers the date -- late invoice in an open one, or a new one.
"""

import re

from tarik.core.app import log, ok, question, server, with_ids
from tarik.core.experio import (
    dry_run,
    experio_refusal,
    fill_invoice,
    in_dossier,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)
from tarik.core.text import normalized
from tarik.invoices.check import resolve_date

NEW = ("nouveau", "nouvel", "nouvelle", "creer", "cree", "creez", "new")  # a new exercice is asked for


def exercice_choice(answer, opens):
    """What the client chose: ("old", exercice) to book it late in an open exercice, ("new", None)
    for a new exercice, or None when unclear (a year or a name is enough)."""
    text = normalized(answer)
    named = [e for e in opens if normalized(e["name"]) in text]
    years = set(re.findall(r"\b(?:19|20)\d\d\b", text))  # a year 1900-2099 as a whole word
    by_year = [e for e in opens if {e["date_start"][:4], e["date_end"][:4]} & years]
    chosen = named or by_year
    if any(word in text.split() for word in NEW):  # "pas de nouvel exercice, mettez-la dans 2024": both said, ask again
        return None if chosen else ("new", None)
    return ("old", chosen[0]) if len(chosen) == 1 else None


# A date with its 4-digit year, day first (15/03/2026) or year first (2026-03-15), any separator,
# not inside a longer number.
DATE_GIVEN = re.compile(r"(?<!\d)(?:\d{1,2}\D\d{1,2}\D\d{4}|\d{4}\D\d{1,2}\D\d{1,2})(?!\d)")


def given_date(answer):
    """The invoice date the client gave: exactly one date, with its 4-digit year, else None (asked again)."""
    found = DATE_GIVEN.findall(answer or "")
    return resolve_date(found[0], "fr") if len(found) == 1 else None


def open_exercice(company_id, year):
    """A new calendar exercice ``year`` (str) in the dossier, opened: Experio's own path, created in draft, its
    rules checked, then opened by action_open. Its id; raises Experio's refusal."""
    # The name is Experio's own formula (_compute_name), which doesn't run before the required check over RPC.
    values = {
        "company_id": company_id,
        "date_start": f"{year}-01-01",
        "date_end": f"{year}-12-31",
        "name": f"Exercice {year}",
    }
    exercice_id = odoo_json("exp.fiscal.entries", "create", values, context=in_dossier(company_id))
    odoo_json("exp.fiscal.entries", "action_open", [exercice_id], context=in_dossier(company_id))
    return exercice_id


def asked(invoice, opens):
    """The question: book ``invoice`` late in one of ``opens``, or a new exercice."""
    names = [e["name"] for e in opens]
    return (
        f"Elle est datee du {invoice['invoice_date']}, en dehors des exercices ouverts ({', '.join(names)}). "
        "Je la comptabilise dans un de ceux-la (comme facture anterieure, precisez lequel), ou je cree un nouvel "
        "exercice couvrant sa vraie date (repondez 'nouveau') ?"
    )


def relink(invoice_id, invoice, exercice):
    """Link the invoice to ``exercice`` (the open one covering its date), when it isn't yet: ok."""
    linked = invoice["exercice_id"]
    if linked and linked[0] == exercice["id"]:
        return ok(f"Facture {invoice_id} : exercice {exercice['name']}.")
    # The exercice was created after the invoice: Experio's compute only reruns when the date changes.
    with refused(
        f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu la rattacher a l'exercice."
    ):
        fill_invoice(invoice_id, {"exercice_id": exercice["id"]}, in_dossier(invoice["company_id"][0]))
    return ok(f"{test_mode_note()}Facture {invoice_id} : rattachee a l'exercice {exercice['name']}.")


def book_late(invoice_id, company_id, exercice):
    """Book the invoice as a late invoice (facture anterieure) in open ``exercice``: ok, with a warning when the
    dossier has no late-invoice account."""
    head = test_mode_note()
    account = odoo_call("res.company", "read", [company_id], fields=["old_invoice_account_id"])[0][
        "old_invoice_account_id"
    ]
    warning = (
        ""
        if account
        else " Aucun compte de factures anterieures dans le dossier (Configuration > Achat > "
        "General Achat) : le compte actuel est garde."
    )
    if not dry_run():
        with refused(
            f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio a refuse la facture anterieure."
        ):
            # Experio keeps the real date, books it at the exercice's start, switches to the late-invoice account.
            context = {"cron_fiscal_entry_id": exercice["id"], **in_dossier(company_id)}
            odoo_json("exp.invoice", "action_activate_old_invoice", [invoice_id], context=context)
    return ok(f"{head}Facture {invoice_id} : facture anterieure dans l'exercice {exercice['name']}.{warning}")


def new_exercice(invoice_id, invoice, opens):
    """Open a new exercice for the invoice's year and link the invoice to it: ok, or a question offering
    ``opens`` instead when Experio refuses it."""
    head = test_mode_note()
    year = invoice["invoice_date"][:4]
    if dry_run():
        return ok(f"{head}Facture {invoice_id} : un exercice {year} ({year}-01-01 au {year}-12-31) serait cree.")
    try:
        exercice_id = open_exercice(invoice["company_id"][0], year)
        fill_invoice(invoice_id, {"exercice_id": exercice_id}, invoice_context(invoice))
    except Exception as error:  # not refused(): Experio's reason becomes a question to the client
        log(f"settle_exercice {invoice_id}: {error}")
        return question(
            f"Facture {invoice_id} : Experio refuse de creer l'exercice {year} ({experio_refusal(error)}). "
            f"La comptabiliser plutot comme facture anterieure dans un exercice ouvert ? "
            f"({', '.join(e['name'] for e in opens)})"
        )
    return ok(f"Facture {invoice_id} : exercice {year} cree et ouvert, la facture y est rattachee.")


@server.tool()
@with_ids
def settle_exercice(invoice_id: int, answer: str = "") -> dict:
    """Make sure a draft invoice is booked in an open fiscal year. When none covers its
    date, the answer is a question (late invoice in an open exercice, or a new one);
    call again with the client's reply as ``answer``."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "invoice_date", "is_old_invoice"])
    require_draft(invoice_id, invoice, "changer d'exercice")
    if not invoice["invoice_date"]:  # the answer to "Quelle est sa date ?" is its date, written as the client wrote it
        given = given_date(answer)
        if not given:
            return question(
                f"Facture {invoice_id} : pas de date, impossible de choisir l'exercice. "
                "Quelle est sa date (jj/mm/aaaa) ?"
            )
        unsaved = f"Facture {invoice_id} : Experio n'a pas pu enregistrer sa date."
        with refused(f"settle_exercice {invoice_id}", unsaved):
            fill_invoice(invoice_id, {"invoice_date": given}, invoice_context(invoice))
        invoice["invoice_date"] = given
        answer = ""
    company_id = invoice["company_id"][0]
    opens = odoo_call(
        "exp.fiscal.entries",
        "search_read",
        [("company_id", "=", company_id), ("state", "=", "open")],
        fields=["id", "name", "date_start", "date_end"],
        order="date_start",
    )
    covering = [e for e in opens if e["date_start"] <= invoice["invoice_date"] <= e["date_end"]]
    if covering and not answer:
        return relink(invoice_id, invoice, covering[0])
    if invoice["is_old_invoice"]:
        return ok(f"Facture {invoice_id} : deja comptabilisee comme facture anterieure.")
    if not answer:
        return question(f"Facture {invoice_id} : {asked(invoice, opens)}")
    choice = exercice_choice(answer, opens)
    if not choice:
        return question(f"Reponse non comprise. Facture {invoice_id} : {asked(invoice, opens)}")
    if choice[0] == "old":
        return book_late(invoice_id, company_id, choice[1])
    return new_exercice(invoice_id, invoice, opens)
