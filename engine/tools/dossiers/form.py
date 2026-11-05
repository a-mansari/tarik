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

Left to Experio's own screens: "hors champ" and the TVA frequency of a dossier that has declarations
(identity.frequency_closed), and the payment delays (set from the turnover, res_config_settings.py execute). The model
dossier's configuration is not on the form: that copy must come before any import (setup.py), which the form can't
promise. Tarik's duplicate check of an ICE or IF is stricter than Experio's, which a cabinet may switch off
(ignore_partner_duplicated_ice_if)."""

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.queries import Q
from engine.tools.dossiers import guide
from engine.tools.dossiers.behaviour import dossier_values, set_for_dossier
from engine.tools.dossiers.identity import (
    CHOICES,
    EDITABLE,
    FOLDER_TYPES,
    PLACEHOLDER_IDS,
    changes_to,
    conflict,
    frequency_closed,
    require_choice,
    require_folder_type,
    sentence,
)
from engine.tools.dossiers.people import sender_of

KEEP = ""  # the TVA choice that changes nothing
FIELDS = ("folder_type", "ice", "if_number", "declaration_mode", "regime_type")  # Experio's, in the form's order
LABELS = {key: sentence(EDITABLE[key][1]) for key in FIELDS} | {
    "folder_type": "Type de dossier",
    "ice": "ICE (15 chiffres)",
}
GONE = "Ce formulaire n'existe pas, a expiré ou a déjà été rempli : demandez-en un nouveau à Tarik."
TAKEN = "Cet ICE ou cet IF est déjà utilisé par un autre dossier : écrivez à Tarik pour le régler."
REFUSED = "Experio a refusé ces réglages : vérifiez les valeurs choisies, ou écrivez à Tarik."


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


def stored_guide(dossier_id):
    """Return the guide's answers kept for dossier ``dossier_id``: {question id: value}."""
    with cabinet_db() as db:
        rows = db.execute(Q.dossier_behaviour_listed, (dossier_id,)).fetchall()
    return {key.removeprefix(guide.KEY): value for key, value in rows if key.startswith(guide.KEY)}


def experio_step(dossier_id, have):
    """Return the form's two first steps, what Experio keeps for the dossier: its identity, then its TVA."""
    who = {"id": "identite", "tag": "", "title": "Le dossier", "hint": "Ce que contient Experio pour ce dossier."}
    tva = {"id": "tva", "tag": "", "title": "TVA", "hint": "Les réglages de TVA se changent dans Experio."}
    controls = []
    for key in FIELDS:
        item = {"key": key, "label": LABELS[key]}
        if key == "folder_type":
            options = [{"key": value, "label": label} for value, label in FOLDER_TYPES.items()]
            control = {**item, "kind": "one", "options": options, "selected": [have[key] or "company"]}
        elif key == "declaration_mode" and frequency_closed(dossier_id, have[key]):
            control = {**item, "kind": "fixed", "value": CHOICES[key].get(have[key], "hors champ")}
        elif key in CHOICES:
            options = [{"key": KEEP, "label": "inchangé"}] + [{"key": v, "label": w} for v, w in CHOICES[key].items()]
            control = {
                **item,
                "kind": "one",
                "options": options,
                "selected": [have[key] if have[key] in CHOICES[key] else KEEP],
            }
        else:
            control = {**item, "kind": "fixed" if locked(have, key) else "text", "value": have[key]}
        controls.append(control)
    return [{**who, "controls": controls[:3]}, {**tva, "controls": controls[3:]}]


def guide_steps(dossier_id):
    """Return the guide's steps, one per question, each holding what the dossier has kept (post_invoices, when on,
    counts as its "post_clear" option)."""
    stored, follows = stored_guide(dossier_id), dossier_values(dossier_id)["post_invoices"]
    steps = []
    for number, question in enumerate(guide.QUESTIONS, 1):
        title, hint = guide.SECTIONS[question["section"]]
        kept = stored.get(question["id"], {"sel": [], "other": "", "note": ""})
        selected = [key for key in kept["sel"] if (question["id"], key) != guide.POST_CLEAR]
        if (question["id"], guide.POST_CLEAR[1]) == guide.POST_CLEAR and follows:
            selected.append(guide.POST_CLEAR[1])
        control = {
            "key": question["id"],
            "label": question["q"],
            "why": question.get("why", ""),
            "kind": question["type"],
            "options": question["options"],
            "selected": selected,
            "other": kept["other"],
            "note": kept["note"],
        }
        steps.append(
            {
                "id": question["id"],
                "tag": question["section"],
                "number": number,
                "title": title,
                "hint": hint,
                "controls": [control],
            }
        )
    return steps


def form_spec(token):
    """Return what the form of ``token`` shows: {dossier: its name, steps: [{id, tag, title, hint, controls}]}. A
    control is {key, label, kind: "one", "many", "text" or "fixed", options: [{key, label, ...}], selected: [keys],
    value (text and fixed), other, note}. First Experio's: the dossier's identity and TVA as Experio has them (KEEP,
    "inchangé", for a TVA setting that is none of the choices; an ICE or IF it has, or a frequency Experio keeps
    closed, is "fixed"), then the guide's questions at what the dossier has kept. Raises ToolError as open_form does."""
    dossier_id, _ = open_form(token)
    name = require_dossier(dossier_id)
    have = company(dossier_id)
    return {"dossier": name, "steps": [*experio_step(dossier_id, have), *guide_steps(dossier_id)]}


def digits(text, label, size=None):
    """Return ``text`` (stripped) when it is only digits, of ``size`` when given, or Experio's own placeholder; "" when
    empty. Raises ToolError naming ``label`` otherwise."""
    text = (text or "").strip()
    if not text or text in PLACEHOLDER_IDS or (text.isdigit() and (size is None or len(text) == size)):
        return text
    raise ToolError(f"{label} : des chiffres seulement{f', {size}' if size else ''}.")


def first(answers, key):
    """Return the one text posted for ``key`` ("" when none)."""
    return (guide.as_list(answers.get(key)) or [""])[0]


def read(answers, have, now, stored):
    """Return (identity, own, lines) for the form's ``answers`` ({field key: text or list}) over the dossier as Experio
    has it, ``have``, Tarik's setting as it follows, ``now`` (dossier_values), and the guide's answers kept, ``stored``:
    the Experio fields to change ({tool argument: value}, for changes_to to keep those that differ), what Tarik keeps
    for the dossier ({key: value}: the guide's answers that differ, and post_invoices when the question on what it may
    validate alone says otherwise than the dossier follows), and what the client chose, one line each. Raises ToolError
    for a value no field takes."""
    identity, own, lines = {}, {}, []
    if first(answers, "folder_type"):
        require_folder_type(first(answers, "folder_type"))
        identity["folder_type"] = first(answers, "folder_type")
        lines.append(f"{LABELS['folder_type']} : {FOLDER_TYPES[identity['folder_type']]}")
    for key, size in (("ice", 15), ("if_number", None)):
        value = digits(first(answers, key), LABELS[key], size)
        if value and not locked(have, key):
            identity[key] = value
            lines.append(f"{EDITABLE[key][1]} : {value}")
    for key in CHOICES:
        if first(answers, key):
            require_choice(key, first(answers, key))
            identity[key] = first(answers, key)
            lines.append(f"{LABELS[key]} : {CHOICES[key][identity[key]]}")
    kept, said = guide.read(answers, stored)
    own.update({f"{guide.KEY}{question}": value for question, value in kept.items()})
    lines += said
    validated = [key for key in guide.as_list(answers.get(guide.POST_CLEAR[0])) if key]
    if validated and now["post_invoices"] != (guide.POST_CLEAR[1] in validated):  # that question answered
        own["post_invoices"] = guide.POST_CLEAR[1] in validated
    return identity, own, lines


def fill_form(token, answers):
    """Apply ``answers`` ({field key: text, or a list of texts for a question of several choices}, as the page posts
    them) to the dossier of the form of ``token``, then close the form. Returns what the client chose, one line each,
    in plain French (in test mode, nothing written nor closed).

    Every answer is checked before anything is written; the form is then taken (once), Experio written with what
    differs, and the form given back when Experio refuses. Raises ToolError when the form is not open, a value is not
    one the field takes, another dossier has that ICE or IF, or Experio refuses."""
    dossier_id, session_id = open_form(token)
    dossier = require_dossier(dossier_id)
    stored = stored_guide(dossier_id)
    identity, own, lines = read(answers, company(dossier_id), dossier_values(dossier_id), stored)
    current, changes = changes_to(dossier_id, identity)
    if "declaration_mode" in changes and frequency_closed(dossier_id, current["declaration_mode"]):
        raise ToolError(f"{LABELS['declaration_mode']} : cela se règle dans Experio, pas ici.")
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
