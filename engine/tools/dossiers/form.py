"""The form a client gets for a dossier just created: one page to choose its identity and its dossier settings
(behaviour.py, scope dossier) at once, instead of Tarik asking them one by one by email (the link: form_link.py;
the page: server/api/form.py).

form_spec says what the page shows, fill_form applies an answer. The page shows what the dossier has in Experio (an
ICE or IF already set stays as it is, shown but not editable) and Tarik's own setting for it; filling it writes to
Experio (res.company, as update_dossier does) only what the client changed, and keeps Tarik's own per dossier. The
token is taken first, so a form is filled once; Experio's refusal gives it back. The page is open to anyone holding the
link: what it says back never names another dossier nor repeats Experio's reason.

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
    EDITABLE,
    FOLDER_TYPES,
    PLACEHOLDER_IDS,
    changes_to,
    conflict,
    require_folder_type,
)
from engine.tools.dossiers.people import sender_of

YES, NO, KEEP = "oui", "non", ""
LABELS = {"folder_type": "Type de dossier", "ice": f"{EDITABLE['ice'][1]} (15 chiffres)", "if_number": "IF"}
GONE = "Ce formulaire n'existe pas, a expiré ou a déjà été rempli : demandez-en un nouveau à Tarik."
TAKEN = "Cet ICE ou cet IF est déjà utilisé par un autre dossier : écrivez à Tarik pour le régler."
REFUSED = "Experio a refusé ces réglages : vérifiez l'ICE et l'IF, ou écrivez à Tarik."
EXPERIO_SETTINGS = {key: about for key, about in SETTINGS.items() if about.get("experio")}


def open_form(token):
    """Return (dossier_id, session_id) of the form of ``token`` while it is open (not filled, not expired). Raises
    ToolError when it is not: unknown, expired or already filled."""
    with cabinet_db() as db:
        found = db.execute(Q.form_open, (token,)).fetchone()
    if not found:
        raise ToolError(GONE)
    return found


def company(dossier_id):
    """Return dossier ``dossier_id``'s identity and Experio-kept settings as Experio has them (res.company read)."""
    fields = ["folder_type", "ice", "vat", *(about["experio"] for about in EXPERIO_SETTINGS.values())]
    with refused(f"form company {dossier_id}", "Experio n'a pas pu lire le dossier."):
        return odoo_call("res.company", "read", [dossier_id], fields=fields)[0]


def locked(have, field):
    """True when the dossier's ``field`` (res.company's) is already a real value: the form leaves it as it is."""
    return bool(have[field]) and have[field] not in PLACEHOLDER_IDS


def form_spec(token):
    """Return what the form of ``token`` shows: {dossier: its name, fields: [{key, label, kind: "choice", "text" or
    "fixed", options: [(value, label)], value}]}. An ICE or IF the dossier has is "fixed"; a setting Experio keeps
    starts at the dossier's own value (KEEP, "inchangé", when it is none of the choices); a Tarik setting at the
    dossier's, else the cabinet's. Raises ToolError as open_form does."""
    dossier_id, _ = open_form(token)
    name = require_dossier(dossier_id)
    have = company(dossier_id)
    fields = [
        {
            "key": "folder_type",
            "label": LABELS["folder_type"],
            "kind": "choice",
            "options": list(FOLDER_TYPES.items()),
            "value": have["folder_type"] or "company",
        }
    ]
    for key, field in (("ice", "ice"), ("if_number", "vat")):
        kind = "fixed" if locked(have, field) else "text"
        fields.append({"key": key, "label": LABELS[key], "kind": kind, "value": have[field] or ""})
    own = dossier_values(dossier_id)
    for key, about in SETTINGS.items():
        if about["scope"] != "dossier":
            continue
        if about.get("experio"):
            options = [(KEEP, "inchangé"), *about["choices"].items()]
            value = have[about["experio"]] if have[about["experio"]] in about["choices"] else KEEP
        else:
            options, value = [(YES, YES), (NO, NO)], YES if own[key] else NO
        fields.append(
            {
                "key": key,
                "label": about["name"],
                "help": about["description"],
                "kind": "choice",
                "options": options,
                "value": value,
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
    """Return (identity, kept, own, lines) for the form's ``answers`` ({field key: text}) over the dossier as it is,
    ``have`` in Experio and ``now`` for Tarik's settings (dossier_values): the identity to change ({tool argument:
    value}), the Experio settings that differ ({res.company field: value}), Tarik's own that differ ({key: value}: one
    left as shown is not kept, so the dossier goes on following the cabinet's), and what the client chose, one line
    each. Raises ToolError for a value no field takes."""
    identity, kept, own, lines = {}, {}, {}, []
    if answers.get("folder_type"):
        require_folder_type(answers["folder_type"])
        identity["folder_type"] = answers["folder_type"]
        lines.append(f"{LABELS['folder_type']} : {FOLDER_TYPES[answers['folder_type']]}")
    for key, field, size in (("ice", "ice", 15), ("if_number", "vat", None)):
        value = digits(answers.get(key), LABELS[key], size)
        if value and not locked(have, field):
            identity[key] = value
            lines.append(f"{EDITABLE[key][1]} : {value}")
    for key, about in SETTINGS.items():
        if about["scope"] != "dossier" or not answers.get(key):
            continue
        value = checked(key, answers[key])
        lines.append(f"{about['name']} : {about['choices'][value] if about['kind'] == 'choice' else answers[key]}")
        if not about.get("experio"):
            if now[key] != value:
                own[key] = value
        elif have[about["experio"]] != value:
            kept[about["experio"]] = value
    return identity, kept, own, lines


def fill_form(token, answers):
    """Apply ``answers`` ({field key: text}, as the page posts them) to the dossier of the form of ``token``, then close
    the form. Returns what the client chose, one line each, in plain French (in test mode, nothing written nor closed).

    Every answer is checked before anything is written; the form is then taken (once), Experio written, and the form
    given back when Experio refuses. Raises ToolError when the form is not open, a value is not one the field takes,
    another dossier has that ICE or IF, or Experio refuses."""
    dossier_id, session_id = open_form(token)
    dossier = require_dossier(dossier_id)
    have = company(dossier_id)
    identity, kept, own, lines = read(answers, have, dossier_values(dossier_id))
    _, changes = changes_to(dossier_id, identity)
    if changes and conflict(dossier_id, dossier, changes):
        raise ToolError(TAKEN)
    if dry_run():
        return [f"{TEST_MODE}{line}" for line in lines]
    with cabinet_db() as db:
        if not db.execute(Q.form_filled, (token,)).fetchone():
            raise ToolError(GONE)
    values = {**changes, **kept}
    try:
        if values:
            with refused(f"fill_form {dossier_id}", REFUSED):
                odoo_json("res.company", "write", [dossier_id], values)
        author = sender_of(session_id) or f"formulaire du dossier {dossier_id}"
        for key, value in own.items():
            set_for_dossier(dossier_id, key, value, author)
    except Exception:
        with cabinet_db() as db:
            db.execute(Q.form_released, (token,))
        raise
    return lines
