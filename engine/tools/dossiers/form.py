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
from engine.tools.dossiers.behaviour import dossier_rows, dossier_values, set_many_for_dossier
from engine.tools.dossiers.form_reply import confirm
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


def stored_guide(own):
    """Return the guide's answers among what is kept for a dossier (``own``: its dossier_rows): {question id: value}."""
    return {key.removeprefix(guide.KEY): value for key, value in own.items() if key.startswith(guide.KEY)}


def experio_steps(dossier_id, have):
    """Return the form's two first steps, what Experio keeps for the dossier: its identity, then its TVA."""
    controls = {}
    for key in FIELDS:
        control = {"key": key, "label": LABELS[key], "kind": "one"}
        if key == "folder_type":
            options = [{"key": k, "label": v} for k, v in FOLDER_TYPES.items()]
            control |= {"options": options, "selected": [have[key] or "company"]}
        elif key == "declaration_mode" and frequency_closed(dossier_id, have[key]):
            control |= {"kind": "fixed", "value": CHOICES[key].get(have[key], "hors champ")}
        elif key in CHOICES:
            options = [{"key": KEEP, "label": "inchangé"}] + [{"key": v, "label": w} for v, w in CHOICES[key].items()]
            control |= {"options": options, "selected": [have[key] if have[key] in CHOICES[key] else KEEP]}
        else:
            control |= {"kind": "fixed" if locked(have, key) else "text", "value": have[key]}
        controls[key] = control
    who = ("folder_type", "ice", "if_number")
    return [
        {
            "tag": "",
            "title": "Le dossier",
            "hint": "Ce que contient Experio pour ce dossier.",
            "controls": [controls[k] for k in who],
        },
        {
            "tag": "",
            "title": "TVA",
            "hint": "Les réglages de TVA se changent dans Experio.",
            "controls": [controls[k] for k in FIELDS if k not in who],
        },
    ]


def guide_steps(stored, follows):
    """Return the guide's steps, one per question, each holding what the dossier has kept (``stored``: stored_guide) and
    ``follows``, whether the dossier posts its verified invoices unasked: it counts as the "post_clear" option."""
    steps = []
    for question in guide.QUESTIONS:
        kept = stored.get(question["id"], {"sel": [], "other": "", "note": ""})
        posts = question["id"] == guide.POST_QUESTION
        selected = [key for key in kept["sel"] if not (posts and key == guide.POST_OPTION)]
        if posts and follows:
            selected.append(guide.POST_OPTION)
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
        steps.append({"tag": question["section"], "title": guide.SECTIONS[question["section"]], "controls": [control]})
    return steps


def form_spec(token):
    """Return what the form of ``token`` shows: {dossier: its name, steps: [{id, tag, title, hint, controls}]}. A
    control is {key, label, kind: "one", "many", "text" or "fixed", options: [{key, label, ...}], selected: [keys],
    value (text and fixed), other, note}. First Experio's: the dossier's identity and TVA as Experio has them (KEEP,
    "inchangé", for a TVA setting that is none of the choices; an ICE or IF it has, or a frequency Experio keeps
    closed, is "fixed"), then the guide's questions at what the dossier has kept. Raises ToolError as open_form does."""
    dossier_id, _ = open_form(token)
    name = require_dossier(dossier_id)
    have, own = company(dossier_id), dossier_rows(dossier_id)
    follows = dossier_values(dossier_id, own)["post_invoices"]
    return {"dossier": name, "steps": [*experio_steps(dossier_id, have), *guide_steps(stored_guide(own), follows)]}


def digits(text, label, size=None):
    """Return ``text`` (stripped) when it is only digits, of ``size`` when given, or Experio's own placeholder; "" when
    empty. Raises ToolError naming ``label`` otherwise."""
    text = (text or "").strip()
    if not text or text in PLACEHOLDER_IDS or (text.isdigit() and (size is None or len(text) == size)):
        return text
    raise ToolError(f"{label} : des chiffres seulement{f', {size}' if size else ''}.")


def read(answers, have, now, stored):
    """Return (identity, own, lines) for the form's ``answers`` ({field key: text or list}) over the dossier as Experio
    has it, ``have``, Tarik's setting as it follows, ``now`` (dossier_values), and the guide's answers kept, ``stored``:
    the Experio fields to change ({tool argument: value}, for changes_to to keep those that differ), what Tarik keeps
    for the dossier ({key: value}: the guide's answers that differ, and post_invoices when the question on what it may
    validate alone says otherwise than the dossier follows), what the client chose, one line each, and ``parts``
    ({"labelled": {identity key: its line}, "recorded": the guide's lines}) for the confirmation. Raises ToolError for a
    value no field takes."""
    identity, own, lines, labelled = {}, {}, [], {}
    if guide.first(answers, "folder_type"):
        require_folder_type(guide.first(answers, "folder_type"))
        identity["folder_type"] = guide.first(answers, "folder_type")
        labelled["folder_type"] = f"{LABELS['folder_type']} : {FOLDER_TYPES[identity['folder_type']]}"
    for key, size in (("ice", 15), ("if_number", None)):
        value = digits(guide.first(answers, key), LABELS[key], size)
        if value and not locked(have, key):
            identity[key] = value
            labelled[key] = f"{EDITABLE[key][1]} : {value}"
    for key in CHOICES:
        if guide.first(answers, key):
            require_choice(key, guide.first(answers, key))
            identity[key] = guide.first(answers, key)
            labelled[key] = f"{LABELS[key]} : {CHOICES[key][identity[key]]}"
    lines = list(labelled.values())
    kept, said, picked = guide.read(answers, stored)
    own.update({f"{guide.KEY}{question}": value for question, value in kept.items()})
    lines += said
    if guide.POST_QUESTION in picked:  # that question answered, or cleared: the flag follows it
        posts = guide.POST_OPTION in picked[guide.POST_QUESTION]
        if now["post_invoices"] != posts:
            own["post_invoices"] = posts
    return identity, own, lines, {"labelled": labelled, "recorded": said}


class Done(list):
    """What a filled form chose, one line each (a list), and ``confirmed``: whether Tarik also wrote it back to the
    client in their conversation (form_reply)."""

    confirmed = False


def fill_form(token, answers):
    """Apply ``answers`` ({field key: text, or a list of texts for a question of several choices}, as the page posts
    them) to the dossier of the form of ``token``, then close the form and tell the client in their conversation.
    Returns what the client chose, one line each, in plain French, as a Done (in test mode, nothing written, closed nor
    said).

    Every answer is checked before anything is written; the form is then taken (once), Experio written with what
    differs, and the form given back when Experio refuses. Raises ToolError when the form is not open, a value is not
    one the field takes, another dossier has that ICE or IF, or Experio refuses."""
    dossier_id, session_id = open_form(token)
    dossier = require_dossier(dossier_id)
    kept = dossier_rows(dossier_id)
    identity, own, lines, parts = read(
        answers, company(dossier_id), dossier_values(dossier_id, kept), stored_guide(kept)
    )
    current, changes = changes_to(dossier_id, identity)
    if "declaration_mode" in changes and frequency_closed(dossier_id, current["declaration_mode"]):
        raise ToolError(f"{LABELS['declaration_mode']} : cela se règle dans Experio, pas ici.")
    if changes and conflict(dossier_id, dossier, changes):
        raise ToolError(TAKEN)
    if dry_run():
        return Done(f"{TEST_MODE}{line}" for line in lines)
    with cabinet_db() as db:
        if not db.execute(Q.form_filled, (token,)).fetchone():
            raise ToolError(GONE)
    try:
        if changes:
            with refused(f"fill_form {dossier_id}", REFUSED):
                odoo_json("res.company", "write", [dossier_id], changes)
        author = sender_of(session_id) or f"formulaire du dossier {dossier_id}"
        if own:
            set_many_for_dossier(dossier_id, own, author)
    except Exception:
        with cabinet_db() as db:
            db.execute(Q.form_released, (token,))
        raise
    done = Done(lines)
    applied = [line for key, line in parts["labelled"].items() if EDITABLE[key][0] in changes]
    done.confirmed = confirm(session_id, dossier, applied, parts["recorded"], own.get("post_invoices"))
    return done
