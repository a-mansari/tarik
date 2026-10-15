"""Phase 13: the fiscal year (exercice) the invoice is booked in.

Experio links the invoice to the exercice covering its date itself (exercice_id is computed
from invoice_date), books a late invoice with its own action_activate_old_invoice, and opens
an exercice through action_open after checking its rules (follows the previous one, at most
12 months, no overlap). tarik-master created exercices straight in the "open" state, past
those checks. Left here: relinking when the exercice came after the invoice, and the
question when no open exercice covers the date -- late invoice in an open one, or a new one.
"""

import re

from tarik.core.app import ok, question, server, with_ids
from tarik.dossiers.search import normalized
from tarik.core.experio import (dry_run, experio_refusal, fill_invoice, invoice_context, odoo_call, odoo_json, refused,
                                require_draft, require_invoice, test_mode_note)

NEW = ("nouveau", "nouvel", "nouvelle", "creer", "cree", "creez", "new")


def exercice_choice(answer, opens):
    """What the client chose: ("old", exercice) to book it late in an open exercice, ("new", None)
    for a new exercice, or None when unclear (tarik-master asked a model; a year or a name is enough)."""
    text = normalized(answer)
    if any(word in text.split() for word in NEW):
        return ("new", None)
    named = [e for e in opens if normalized(e["name"]) in text]
    years = set(re.findall(r"\b(?:19|20)\d\d\b", text))
    by_year = [e for e in opens if {e["date_start"][:4], e["date_end"][:4]} & years]
    chosen = named or by_year
    return ("old", chosen[0]) if len(chosen) == 1 else None


def open_exercice(company_id, year):
    """A new calendar exercice, opened: Experio's own path, created in draft, its rules
    checked, then opened by action_open. Raises Experio's refusal."""
    # The name is Experio's own formula (_compute_name), which doesn't run before the required check over RPC.
    values = {"company_id": company_id, "date_start": f"{year}-01-01", "date_end": f"{year}-12-31", "name": f"Exercice {year}"}
    exercice_id = odoo_json("exp.fiscal.entries", "create", values, context={"allowed_company_ids": [company_id]})
    odoo_json("exp.fiscal.entries", "action_open", [exercice_id], context={"allowed_company_ids": [company_id]})
    return exercice_id


def asked(invoice, opens):
    """tarik-master's question, word for word."""
    names = [e["name"] for e in opens]
    return (f"Elle est datee du {invoice['invoice_date']}, en dehors des exercices ouverts ({', '.join(names)}). "
            "Je la comptabilise dans un de ceux-la (comme facture anterieure, precisez lequel), ou je cree un nouvel "
            "exercice couvrant sa vraie date (repondez 'nouveau') ?")


@server.tool()
@with_ids
def settle_exercice(invoice_id: int, answer: str = "") -> dict:
    """Make sure a draft invoice is booked in an open fiscal year. When none covers its
    date, the answer is a question (late invoice in an open exercice, or a new one);
    call again with the client's reply as ``answer``."""
    invoice = require_invoice(invoice_id, ["state", "company_id", "exercice_id", "invoice_date", "is_old_invoice"])
    require_draft(invoice_id, invoice, "changer d'exercice")
    if not invoice["invoice_date"]:
        return question(f"Facture {invoice_id} : pas de date, impossible de choisir l'exercice. Quelle est sa date ?")
    company_id = invoice["company_id"][0]
    opens = odoo_call("exp.fiscal.entries", "search_read", [("company_id", "=", company_id), ("state", "=", "open")],
                      fields=["id", "name", "date_start", "date_end"], order="date_start")
    when = invoice["invoice_date"]
    covering = [e for e in opens if e["date_start"] <= when <= e["date_end"]]
    if covering and not answer:
        linked = invoice["exercice_id"]
        if linked and linked[0] == covering[0]["id"]:
            return ok(f"Facture {invoice_id} : exercice {covering[0]['name']}.")
        # The exercice was created after the invoice: Experio's compute only reruns when the date changes.
        with refused(f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu la rattacher a l'exercice."):
            fill_invoice(invoice_id, {"exercice_id": covering[0]["id"]}, {"allowed_company_ids": [company_id]})
        return ok(f"{test_mode_note()}Facture {invoice_id} : rattachee a l'exercice {covering[0]['name']}.")
    if invoice["is_old_invoice"]:
        return ok(f"Facture {invoice_id} : deja comptabilisee comme facture anterieure.")
    if not answer:
        return question(f"Facture {invoice_id} : {asked(invoice, opens)}")
    choice = exercice_choice(answer, opens)
    if not choice:
        return question(f"Reponse non comprise. Facture {invoice_id} : {asked(invoice, opens)}")
    head = test_mode_note()
    if choice[0] == "old":
        exercice = choice[1]
        account = odoo_call("res.company", "read", [company_id], fields=["old_invoice_account_id"])[0]["old_invoice_account_id"]
        warning = ("" if account else " Aucun compte de factures anterieures dans le dossier (Configuration > Achat > "
                   "General Achat) : le compte actuel est garde.")
        if not dry_run():
            with refused(f"settle_exercice {invoice_id}", f"Facture {invoice_id} : Experio a refuse la facture anterieure."):
                # Experio keeps the real date, books it at the exercice's start, switches to the late-invoice account.
                odoo_json("exp.invoice", "action_activate_old_invoice", [invoice_id],
                          context={"cron_fiscal_entry_id": exercice["id"], "allowed_company_ids": [company_id]})
        return ok(f"{head}Facture {invoice_id} : facture anterieure dans l'exercice {exercice['name']}.{warning}")
    year = when[:4]
    if dry_run():
        return ok(f"{head}Facture {invoice_id} : un exercice {year} ({year}-01-01 au {year}-12-31) serait cree.")
    try:
        exercice_id = open_exercice(company_id, year)
        fill_invoice(invoice_id, {"exercice_id": exercice_id}, invoice_context(invoice))
    except Exception as error:  # not refused(): Experio's reason becomes a question to the client
        print(f"settle_exercice {invoice_id}: {error}", flush=True)
        return question(f"Facture {invoice_id} : Experio refuse de creer l'exercice {year} ({experio_refusal(error)}). "
                        f"La comptabiliser plutot comme facture anterieure dans un exercice ouvert ? "
                        f"({', '.join(e['name'] for e in opens)})")
    return ok(f"Facture {invoice_id} : exercice {year} cree et ouvert, la facture y est rattachee.")
