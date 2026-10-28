"""The fiscal year (exercice) the invoice is booked in.

Experio links the invoice to the exercice covering its date itself (exp.invoice _compute_exercice_id, from
invoice_date), and books a late invoice with its own action_activate_old_invoice. An exercice's rules are checked
when it is saved (it follows the previous one; at most 12 months except the first) and when action_open opens it
(no overlap with an open or closed one). Left here: relinking when the exercice was created after the invoice, and
the question when no open exercice covers the date: late invoice in an open one, or a new one.
"""

import re
from datetime import date

from engine.tools.core.app import log, ok, question, routine, step_tool
from engine.tools.core.experio import (
    dry_run,
    experio_refusal,
    fill_invoice,
    in_dossier,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_draft_invoice,
    test_mode_note,
)
from engine.tools.core.words import normalized
from engine.tools.invoices.check import resolve_date

FISCAL_YEAR = "exp.fiscal.year.creation.wizard"


def with_fiscal_year(action, context):
    """Return ``action`` (what an action_post answered) once Experio's « Créer l'exercice fiscal » window is answered.

    A posting dated outside every exercice opens that window (check_fiscal_year_creation) instead of posting; its
    confirmation creates the exercice(s) it proposes, open, then posts again (action_create_and_post): the client
    asked for the posting, so Tarik confirms it with Experio's own dates, unless the posting date is more than two years
    from today (a misread scan would open years nobody asked for). Any other answer is returned as it is.
    """
    for _ in range(3):  # Experio's own re-post ends it (a bool or another window); never an endless loop
        if not (isinstance(action, dict) and action.get("res_model") == FISCAL_YEAR and action.get("res_id")):
            break
        wizard_id, fields = action["res_id"], ["move_date", "date_start"]
        wizard = odoo_call(FISCAL_YEAR, "read", [wizard_id], fields=fields, context=context)[0]
        if not wizard["move_date"] or abs(int(wizard["move_date"][:4]) - date.today().year) > 2:
            break  # a date far from today (a misread scan) would open years nobody asked for: the cabinet decides
        if not wizard["date_start"]:  # a dossier with no exercice at all: Experio proposes no dates, the calendar year
            year = wizard["move_date"][:4]
            years = {"date_start": f"{year}-01-01", "date_end": f"{year}-12-31"}
            odoo_call(FISCAL_YEAR, "write", [wizard_id], years, context=context)
        action = odoo_json(FISCAL_YEAR, "action_create_and_post", [action["res_id"]], context=context)
    return action


def exercice_choice(decision, opens):
    """Return the client's choice as the agent read it (``decision``): ("old", exercice) to book the invoice late
    in that one of ``opens`` (named or numbered as the question lists it), ("new", None) for "nouveau", None for
    anything else (asked again)."""
    decision = normalized(str(decision or ""))
    if decision == "nouveau":
        return ("new", None)
    named = [e for e in opens if decision in (normalized(e["name"]), str(e["id"]))]
    return ("old", named[0]) if len(named) == 1 else None


# A date with its 4-digit year, day first (15/03/2026) or year first (2026-03-15), any separator,
# not inside a longer number.
DATE_GIVEN = re.compile(r"(?<!\d)(?:\d{1,2}\D\d{1,2}\D\d{4}|\d{4}\D\d{1,2}\D\d{1,2})(?!\d)")


def given_date(answer):
    """Return the invoice date the client gave in ``answer`` as YYYY-MM-DD: exactly one date with its 4-digit year,
    day first or year first; else None (asked again)."""
    found = DATE_GIVEN.findall(answer or "")
    return resolve_date(found[0], "fr") if len(found) == 1 else None


def open_exercice(company_id, year):
    """Create the calendar exercice ``year`` (str) in the dossier and open it, Experio's own path: created in
    draft (its rules checked on save), then opened by action_open. Returns its id; Experio's refusal is raised."""
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
    """Return the question text: book ``invoice`` late in one of the open exercices ``opens``, or a new one."""
    names = [f"`{e['name']}`" for e in opens]
    return (
        f"Elle est datée du {invoice['invoice_date']}, en dehors des exercices ouverts ({', '.join(names)}). "
        "Je la comptabilise dans un de ceux-là (comme facture antérieure, précisez lequel), ou je crée un nouvel "
        "exercice couvrant sa vraie date (répondez « nouveau ») ?"
    )


def relink(invoice_id, invoice, exercice):
    """Link the invoice to ``exercice`` (the open one covering its date) unless it already is. Returns an ok
    result when linked now, a routine result when it already was."""
    linked = invoice["exercice_id"]
    if linked and linked[0] == exercice["id"]:
        return routine(f"Facture {invoice_id} : exercice `{exercice['name']}`.")
    # The exercice was created after the invoice: Experio's compute only reruns when the date changes.
    with refused(
        f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu la rattacher à l'exercice."
    ):
        fill_invoice(invoice_id, {"exercice_id": exercice["id"]}, in_dossier(invoice["company_id"][0]))
    return ok(f"{test_mode_note()}Facture {invoice_id} : rattachée à l'exercice `{exercice['name']}`.")


def book_late(invoice_id, company_id, exercice):
    """Book the invoice as a late invoice (facture antérieure) in the open ``exercice``, with Experio's
    action_activate_old_invoice: the real date is kept aside, the invoice dated at the exercice's start and put on
    the dossier's late-invoice account. Returns an ok result, with a warning when the dossier has no such account
    (the current account is kept)."""
    head = test_mode_note()
    company = odoo_call("res.company", "read", [company_id], fields=["old_invoice_account_id"])[0]
    warning = ""
    if not company["old_invoice_account_id"]:
        warning = (
            " Aucun compte de factures anterieures dans le dossier (Configuration > Achat > "
            "General Achat) : le compte actuel est garde."
        )
    if not dry_run():
        with refused(
            f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio a refusé la facture antérieure."
        ):
            # Experio keeps the real date, books it at the exercice's start, switches to the late-invoice account.
            context = {"cron_fiscal_entry_id": exercice["id"], **in_dossier(company_id)}
            odoo_json("exp.invoice", "action_activate_old_invoice", [invoice_id], context=context)
    return ok(f"{head}Facture {invoice_id} : facture antérieure dans l'exercice `{exercice['name']}`.{warning}")


def new_exercice(invoice_id, invoice, opens):
    """Open a new calendar exercice for the invoice's year and link the invoice to it. Returns an ok result, or a
    question result offering a late invoice in one of ``opens`` when Experio refuses the exercice."""
    head = test_mode_note()
    year = invoice["invoice_date"][:4]
    if dry_run():
        return ok(f"{head}Facture {invoice_id} : un exercice {year} ({year}-01-01 au {year}-12-31) serait créé.")
    try:
        exercice_id = open_exercice(invoice["company_id"][0], year)
        fill_invoice(invoice_id, {"exercice_id": exercice_id}, invoice_context(invoice))
    except Exception as error:  # not refused(): Experio's reason becomes a question to the client
        log(f"settle_exercice {invoice_id}: {error}")
        return question(
            f"Facture {invoice_id} : Experio refuse de créer l'exercice {year} ({experio_refusal(error)}). "
            f"La comptabiliser plutôt comme facture antérieure dans un exercice ouvert ? "
            f"({', '.join(f'`{e["name"]}`' for e in opens)})"
        )
    return ok(f"Facture {invoice_id} : exercice {year} créé et ouvert, la facture y est rattachée.")


@step_tool
def settle_exercice(invoice_id: int, answer: str = "", decision: str = "") -> dict:
    """Make sure a draft invoice is booked in an open fiscal year (exercice).

    When no open exercice covers its date, returns a question result (late invoice in an open exercice, or a new
    one); call again with the client's reply as ``answer`` and what the agent read in it as ``decision``:
    "nouveau", or the open exercice chosen, named as the question lists it. An invoice without a date gets a
    question asking for it first; ``answer`` then holds the date."""
    fields = ["state", "company_id", "exercice_id", "invoice_date", "is_old_invoice"]
    invoice = require_draft_invoice(invoice_id, fields, "changer d'exercice")
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
    if covering:  # an exercice opened meanwhile covers it: linked, whatever the client answered
        return relink(invoice_id, invoice, covering[0])
    if invoice["is_old_invoice"]:
        return routine(f"Facture {invoice_id} : déjà comptabilisée comme facture antérieure.")
    if not answer:
        return question(f"Facture {invoice_id} : {asked(invoice, opens)}")
    choice = exercice_choice(decision, opens)
    if not choice:
        return question(f"Réponse non comprise. Facture {invoice_id} : {asked(invoice, opens)}")
    how, exercice = choice
    if how == "old":
        return book_late(invoice_id, company_id, exercice)
    return new_exercice(invoice_id, invoice, opens)
