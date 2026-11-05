"""The form a client gets for a dossier just created: one page to choose its identity, its TVA settings and how Tarik
posts its invoices at once, instead of Tarik asking them one by one by email (the link: form_link.py; the page:
server/api/form.py).

What Experio keeps for the dossier (type, ICE, IF, TVA frequency and regime: identity.EDITABLE) is read from Experio for
the page and written back to Experio only when the client changed it, as update_dossier does: Tarik keeps no copy of it
(nor a default). An ICE or IF already set stays as it is, shown but not editable. Only Tarik's own dossier setting
(post_invoices, behaviour.py) is kept here, per dossier, and only when it differs from what the dossier follows.

form_spec says what the page shows, fill_form applies an answer. The token is taken first, so a form is filled once;
Experio's refusal gives it back. The page is open to anyone holding the link: what it says back never names another
dossier nor repeats Experio's reason.

Left to Experio's own screens: "hors champ" (it flags the dossier's draft purchases non-recoverable, res_company.py
write) and the payment delays (set from the turnover, res_config_settings.py execute). The model dossier's configuration
is not on the form: that copy must come before any import (setup.py), which the form can't promise. Tarik's duplicate
check of an ICE or IF is stricter than Experio's, which a cabinet may switch off (ignore_partner_duplicated_ice_if)."""

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.queries import Q
from engine.tools.dossiers.behaviour import SETTINGS, checked, dossier_values, set_for_dossier
from engine.tools.dossiers.identity import (
    CHOICES,
    EDITABLE,
    FOLDER_TYPES,
    PLACEHOLDER_IDS,
    changes_to,
    conflict,
    require_choice,
    require_folder_type,
)
from engine.tools.dossiers.people import sender_of

YES, NO, KEEP = "oui", "non", ""
FIELDS = ("folder_type", "ice", "if_number", "declaration_mode", "regime_type")  # Experio's, in the form's order
LABELS = {
    "folder_type": "Type de dossier",
    "ice": f"{EDITABLE['ice'][1]} (15 chiffres)",
    "if_number": "IF",
    "declaration_mode": "Déclaration de TVA",
    "regime_type": "Régime de TVA",
}
GONE = "Ce formulaire n'existe pas, a expiré ou a déjà été rempli : demandez-en un nouveau à Tarik."
TAKEN = "Cet ICE ou cet IF est déjà utilisé par un autre dossier : écrivez à Tarik pour le régler."
REFUSED = "Experio a refusé ces réglages : vérifiez l'ICE et l'IF, ou écrivez à Tarik."


def open_form(token):
    """Return (dossier_id, session_id) of the form of ``token`` while it is open (not filled, not expired). Raises
    ToolError when it is not: unknown, expired or already filled."""
    with cabinet_db() as db:
        found = db.execute(Q.form_open, (token,)).fetchone()
    if not found:
        raise ToolError(GONE)
    return found


def company(dossier_id):
    """Return dossier ``dossier_id``'s FIELDS as Experio has them, {tool argument: value} (res.company read)."""
    fields = [EDITABLE[key][0] for key in FIELDS]
    with refused(f"form company {dossier_id}", "Experio n'a pas pu lire le dossier."):
        found = odoo_call("res.company", "read", [dossier_id], fields=fields)[0]
    return {key: found[EDITABLE[key][0]] or "" for key in FIELDS}


def locked(have, key):
    """True when the dossier's ICE or IF ``key`` is already a real value: the form leaves it as it is."""
    return bool(have[key]) and have[key] not in PLACEHOLDER_IDS


def form_spec(token):
    """Return what the form of ``token`` shows: {dossier: its name, fields: [{key, label, kind: "choice", "text" or
    "fixed", options: [(value, label)], value}]}: Experio's FIELDS as the dossier has them (KEEP, "inchangé", for a TVA
    setting that is none of the choices; an ICE or IF it has is "fixed"), then the dossier's own post_invoices (what
    it follows). Raises ToolError as open_form does."""
    dossier_id, _ = open_form(token)
    name = require_dossier(dossier_id)
    have = company(dossier_id)
    fields = []
    for key in FIELDS:
        item = {"key": key, "label": LABELS[key], "value": have[key]}
        if key == "folder_type":
            fields.append(
                {**item, "kind": "choice", "options": list(FOLDER_TYPES.items()), "value": have[key] or "company"}
            )
        elif key in CHOICES:
            options = [(KEEP, "inchangé"), *CHOICES[key].items()]
            fields.append(
                {
                    **item,
                    "kind": "choice",
                    "options": options,
                    "value": have[key] if have[key] in CHOICES[key] else KEEP,
                }
            )
        else:
            fields.append({**item, "kind": "fixed" if locked(have, key) else "text"})
    about = SETTINGS["post_invoices"]
    follows = dossier_values(dossier_id)["post_invoices"]
    fields.append(
        {
            "key": "post_invoices",
            "label": about["name"],
            "help": about["description"],
            "kind": "choice",
            "options": [(YES, YES), (NO, NO)],
            "value": YES if follows else NO,
        }
    )
    return {"dossier": name, "fields": fields}


def digits(text, label, size=None):
    """Return ``text`` (stripped) when it is only digits, of ``size`` when given, or Experio's own placeholder; "" when
    empty. Raises ToolError naming ``label`` otherwise."""
    text = (text or "").strip()
    if not text or text in PLACEHOLDER_IDS or (text.isdigit() and (size is None or len(text) == size)):
        return text
    raise ToolError(f"{label} : des chiffres seulement{f', {size}' if size else ''}.")


def read(answers, have, now):
    """Return (identity, own, lines) for the form's ``answers`` ({field key: text}) over the dossier as Experio has it,
    ``have``, and Tarik's setting as it follows, ``now`` (dossier_values): the Experio fields to change ({tool argument:
    value}, for changes_to to keep those that differ), Tarik's own setting when it differs ({key: value}: one left as
    shown is not kept, so the dossier goes on following the cabinet's), and what the client chose, one line each. Raises
    ToolError for a value no field takes."""
    identity, own, lines = {}, {}, []
    if answers.get("folder_type"):
        require_folder_type(answers["folder_type"])
        identity["folder_type"] = answers["folder_type"]
        lines.append(f"{LABELS['folder_type']} : {FOLDER_TYPES[answers['folder_type']]}")
    for key, size in (("ice", 15), ("if_number", None)):
        value = digits(answers.get(key), LABELS[key], size)
        if value and not locked(have, key):
            identity[key] = value
            lines.append(f"{EDITABLE[key][1]} : {value}")
    for key in CHOICES:
        if answers.get(key):
            require_choice(key, answers[key])
            identity[key] = answers[key]
            lines.append(f"{LABELS[key]} : {CHOICES[key][answers[key]]}")
    if answers.get("post_invoices"):
        value = checked("post_invoices", answers["post_invoices"])
        lines.append(f"{SETTINGS['post_invoices']['name']} : {YES if value else NO}")
        if now["post_invoices"] != value:
            own["post_invoices"] = value
    return identity, own, lines


def fill_form(token, answers):
    """Apply ``answers`` ({field key: text}, as the page posts them) to the dossier of the form of ``token``, then close
    the form. Returns what the client chose, one line each, in plain French (in test mode, nothing written nor closed).

    Every answer is checked before anything is written; the form is then taken (once), Experio written with what
    differs, and the form given back when Experio refuses. Raises ToolError when the form is not open, a value is not
    one the field takes, another dossier has that ICE or IF, or Experio refuses."""
    dossier_id, session_id = open_form(token)
    dossier = require_dossier(dossier_id)
    identity, own, lines = read(answers, company(dossier_id), dossier_values(dossier_id))
    _, changes = changes_to(dossier_id, identity)
    if changes and conflict(dossier_id, dossier, changes):
        raise ToolError(TAKEN)
    if dry_run():
        return [f"{TEST_MODE}{line}" for line in lines]
    with cabinet_db() as db:
        if not db.execute(Q.form_filled, (token,)).fetchone():
            raise ToolError(GONE)
    try:
        if changes:
            with refused(f"fill_form {dossier_id}", REFUSED):
                odoo_json("res.company", "write", [dossier_id], changes)
        author = sender_of(session_id) or f"formulaire du dossier {dossier_id}"
        for key, value in own.items():
            set_for_dossier(dossier_id, key, value, author)
    except Exception:
        with cabinet_db() as db:
            db.execute(Q.form_released, (token,))
        raise
    return lines
