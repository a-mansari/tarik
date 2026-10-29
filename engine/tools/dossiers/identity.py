"""Create a dossier, or change the identity fields at the top of Experio's dossier form (name, type, ICE, IF,
RC...) of an existing one. Only values the client wrote themselves are used; an ICE, IF or name another dossier
already has is refused."""

import re
from typing import Final

from engine.tools.core.app import ToolError, ok, question, step_tool, stop, tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import DOSSIER_DOMAIN, SIMILAR_NAME, TEST_MODE
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import dry_run, main_dossier_context, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.queries import Q
from engine.tools.core.words import ICE_WRITTEN, as_number, normalized, said, similarity, written_numbers
from engine.tools.dossiers.people import attach_user
from engine.tools.dossiers.search import dossiers

FOLDER_TYPES = {"company": "societe", "person": "personne physique", "cpu": "CPU", "st": "ST"}
PLACEHOLDER_IDS = {"1111", "2222"}  # Experio's own "not known yet" ICE/IF, shared by many dossiers

SKIP_QUESTION = (
    "Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous {what} sans cette "
    "verification ? oui / non"
)
SIMILAR_QUESTION = (
    "Un dossier au nom tres proche existe deja ({names}). Faut-il quand meme creer un nouveau dossier `{name}` ? "
    "oui / non"
)

# What a client may change in a dossier: the fields of Experio's dossier form, before its tabs (the
# currency and the logo aside). Tool argument -> Experio field, and how Tarik names it to the client.
EDITABLE = {
    "name": ("name", "nom"),
    "folder_type": ("folder_type", "type"),
    "ice": ("ice", "ICE"),
    "if_number": ("vat", "IF"),
    "rc": ("rc", "RC"),
    "patente": ("pat", "patente"),
    "tsc": ("tsc", "T.S.C"),
    "capital": ("capital", "capital"),
    "cnss": ("cnss_number", "CNSS"),
    "street": ("street", "adresse"),
    "street2": ("street2", "complement d'adresse"),
    "city": ("city", "ville"),
    "zip": ("zip", "code postal"),
}
IDENTIFIERS = {"ice", "if_number", "rc", "patente", "tsc", "cnss", "zip"}  # checked digit for digit
# Between a label and its number, only punctuation and a few linking words ("le RC est 331245", "RC : 331245",
# "IF n° 0712"): never another label, so "RC, IF 12345678" gives the RC nothing.
# Any run of spaces, : = . , ° - or the whole words en, est, devient, par, du, de, le, la, numero, num, n, no.
LINK = r"(?:[\s:=.,°-]|\b(?:en|est|devient|par|du|de|le|la|numero|num|n|no)\b)*"


def labelled(label):
    """Return a compiled regex for the number written right after ``label`` (a regex): the label as a whole word,
    LINK, then its digits (captured), not followed by another digit."""
    return re.compile(rf"\b{label}\b{LINK}(\d+)(?!\d)", re.IGNORECASE)


# The identifiers a client writes after their label: their number counts for that field only ("IF 12345678" is no RC).
# An ICE is told apart by its 15 digits alone.
IF_WRITTEN: Final = labelled(r"I\.?F\.?")
LABELLED = {"ice": ICE_WRITTEN, "if_number": IF_WRITTEN, "rc": labelled(r"R\.?C\.?")}
# "societe" alone is not a type: "l'adresse de ma societe" changes no dossier into a company. (A spreadsheet's Type
# column is another matter: there it is one, management.TYPE_WORDS.)
TYPE_WORDS = {
    "person": ("personne physique", "physique"),
    "company": ("personne morale", "morale"),
    "cpu": ("cpu",),
    "st": ("sole trader", "commercant", "entreprise individuelle"),
}


# --- What the client wrote ---


def joined(text, separators=" .  "):
    """Return ``text`` with each of ``separators`` that sits between two digits removed: "IF 71 234 561" ->
    "IF 71234561"."""
    return re.sub(rf"(?<=\d)[{separators}](?=\d)", "", text or "")  # one separator between two digits


def as_written(value, written, current=None):
    """Return the ICE or IF ``value`` given by the model, as the client wrote it.

    The model may send it as a number, losing leading zeros, or leave it out. Returns, in order: the number of
    ``written`` equal to ``value`` once leading zeros are ignored; for an empty ``value``, the only distinct
    number in ``current``; otherwise ``value`` itself as text (a trailing ".0" removed, "" when None).

    Args:
        written: the numbers found in all the client's messages.
        current: the numbers found in their latest message; None means ``written``.
    """
    current = written if current is None else current
    value = str(value if value is not None else "").strip()
    if value.endswith(".0") and value.removesuffix(".0").isdigit():
        value = value.removesuffix(".0")
    if value.isdigit():
        same = [number for number in written if number.lstrip("0") == value.lstrip("0")]
        return same[0] if same else value
    if not value and len(set(current)) == 1:
        return current[0]
    return value


def written_id(value, pattern, words, latest=None):
    """Return ``value`` (an ICE or IF) as_written by the client: ``pattern`` finds the candidate numbers in
    ``words`` (all the client's messages) and in ``latest`` (their last message; None: use ``words``)."""
    current = None if latest is None else pattern.findall(joined(latest))
    return as_written(value, pattern.findall(joined(words)), current)


def client_wrote(key, value, words):
    """True when the client wrote the new ``value`` for field ``key`` themselves in ``words``: a model may not
    choose nor "correct" one. An identifier must be one of their numbers digit for digit (an ICE as a 15-digit
    number, an IF or RC after its label, a placeholder 1111/2222 as a whole word); the capital one of their
    amounts, decimals kept (100 000,50); the type one of its TYPE_WORDS; a text, whole words of theirs."""
    if key == "capital":
        try:
            amount = as_number(value)
        except ValueError:
            return False
        return any(abs(amount - written) < 0.005 for written in written_numbers(words))
    if key in IDENTIFIERS:
        number = re.sub(r"\.0+$", "", str(value)).replace(" ", "")  # a trailing ".0" a float left
        if number in PLACEHOLDER_IDS:  # Experio's "not known yet", too short for ICE_WRITTEN
            return said(number, normalized(words))
        if key in LABELLED:
            found = LABELLED[key].findall(joined(words))
        else:  # every number in the text, the spaces, dots and commas between its digits removed (100 000 -> 100000)
            found = re.findall(r"\d+", joined(words, " .,  "))
        return number.isdigit() and number in found
    if key == "folder_type":
        return any(said(word, normalized(words)) for word in TYPE_WORDS.get(value, ()))
    return said(normalized(value), normalized(words))


def confirmed(session_id, client_confirmed, *parts):
    """True when the agent read a clear yes (``client_confirmed`` is True) and Tarik's last question in conversation
    ``session_id`` contains every text of ``parts`` (normalized), typically the question's key phrase and the
    dossier's name. A yes to another question, or about another dossier, confirms nothing; nothing is confirmed
    before Tarik asked."""
    with cabinet_db() as db:
        last = db.execute(
            Q.last_question_of_session,
            (session_id,),
        ).fetchone()
    return client_confirmed is True and bool(last) and all(normalized(part) in normalized(last[0]) for part in parts)


# --- Other dossiers ---


def real_ids(pairs):
    """Return the (field, value) of ``pairs`` holding a real ICE or IF, value stripped: neither empty nor a
    placeholder (PLACEHOLDER_IDS)."""
    return [
        (field, value.strip())
        for field, value in pairs
        if value and value.strip() and value.strip() not in PLACEHOLDER_IDS
    ]


def having(keys, *extra):
    """Return the dossiers ([{id, name}]) having any of ``keys`` ([(field, value)], at least one), narrowed by the
    ``extra`` domain terms (each a (field, operator, value) tuple, e.g. ("id", "!=", dossier_id))."""
    either = ["|"] * (len(keys) - 1)
    domain = [*DOSSIER_DOMAIN, *either, *[(field, "=", value) for field, value in keys], *extra]
    return odoo_call("res.company", "search_read", domain, fields=["name"])


def lookalikes(name, ice, if_number):
    """Return (same, similar): the dossiers ([{id, name}]) with this real ICE or IF, and those whose name is at
    least SIMILAR_NAME alike."""
    keys = real_ids((("ice", ice), ("vat", if_number)))
    same = having(keys) if keys else []
    return same, [dossier for dossier in dossiers() if similarity(name, dossier["name"]) >= SIMILAR_NAME]


def require_folder_type(folder_type):
    """Raise ToolError unless ``folder_type`` is one of FOLDER_TYPES."""
    if folder_type not in FOLDER_TYPES:
        raise ToolError(f"Type de dossier inconnu : {', '.join(FOLDER_TYPES)}.")


# --- Creating a dossier ---


def client_ids(ice, if_number, words, client_message):
    """Return (ice, if_number, note): the ICE and IF as the client wrote them (as_written from ``words``, all their
    messages, and ``client_message``), "" for one they did not write; ``note`` is the French sentence naming
    those left out, "" when none."""
    ice = written_id(ice, ICE_WRITTEN, words, client_message)
    if_number = written_id(if_number, IF_WRITTEN, words, client_message)
    given = (("ice", "ICE", ice), ("if_number", "IF", if_number))
    left_out = [label for key, label, value in given if value and not client_wrote(key, value, words)]
    if "ICE" in left_out:
        ice = ""
    if "IF" in left_out:
        if_number = ""
    note = f" Non repris, le client ne l'a pas ecrit : {', '.join(left_out)}." if left_out else ""
    return ice, if_number, note


def created_here(session_id, name):
    """Return the dossier ({id, name}) named ``name`` that this conversation already created, or None."""
    with cabinet_db() as db:
        rows = db.execute(Q.dossiers_created_in_session, (session_id,))
        created = [dossier_id for (dossier_id,) in rows]
    if not created:
        return None
    for dossier in odoo_call("res.company", "read", created, fields=["name"]):
        if normalized(dossier["name"]) == normalized(name):
            return dossier
    return None


def drawn_from_a_file(session_id, name, words):
    """True when ``name`` is no name the client wrote (not in their ``words``) but words of a file they sent: a FEC
    named « FEC ANFA RETAIL.xlsx » makes the agent create « ANFA RETAIL » when the client only said « charge ce
    FEC »."""
    if said(normalized(name), normalized(words)):
        return False
    with cabinet_db() as db:
        files = [file_name for (file_name,) in db.execute(Q.file_names_of_session, (session_id,))]
    return any(said(normalized(name), normalized(file_name.rsplit(".", 1)[0])) for file_name in files)


def duplicate(session_id, client_confirmed, name, ice, if_number):
    """Return a stop result when another dossier has this ICE or IF; a question result while a dossier has a very
    similar name and the client has not confirmed creating it anyway; None when the dossier may be created."""
    same, similar = lookalikes(name, ice, if_number)
    if same:
        return stop(
            f"Dossier `{name}` non cree : un dossier existe deja avec cet ICE ou cet IF "
            f"({', '.join(f'`{d["name"]}`' for d in same)})."
        )
    # The skip question comes only once this dossier's similar name was confirmed: a yes to it confirms both.
    if similar and not (
        confirmed(session_id, client_confirmed, "nom tres proche", name)
        or confirmed(session_id, client_confirmed, "sans cette verification", name)
    ):
        return question(SIMILAR_QUESTION.format(names=", ".join(f"`{d['name']}`" for d in similar), name=name))
    return None


@tool
def create_dossier(
    name: str = "",
    ice: str | int = "",
    if_number: str | int = "",
    folder_type: str = "company",
    skip_ice_if_check: bool = False,
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Create a new dossier in Experio, as the Folders screen does, and put Tarik's user on it.

    Experio refuses a name already used (res.company name_uniq) and checks the ICE and IF against the official
    registry (exp.partner.base _check_ice_if_verification).

    Args:
        ice, if_number: the client's ICE and IF; each is kept only when the client wrote it (``client_message``
            and their earlier messages in ``session_id``).
        folder_type: company (default), person, cpu or st.
        skip_ice_if_check: tick the form's "Ignore ICE/IF Verification"; asked to the client first.
        client_confirmed: the agent's reading of a clear yes to Tarik's last question (skip the check, or
            create despite a very similar name); it confirms that question only.

    Returns:
        - an ok result with ``dossier_id`` (and reused=True) when this conversation already created a dossier
          of that name (MiniMax sometimes asks again, to import its entries);
        - a stop result when a dossier has the same ICE or IF;
        - a question result for a very similar name, or for skipping the check, until confirmed;
        - an ok result with ``dossier_id`` once created (in test mode, without creating it).

    Raises:
        ToolError: no name given, or an unknown ``folder_type``.
    """
    client_message = own_words(client_message)
    name = " ".join((name or "").split())
    if not name:  # MiniMax sometimes calls with no fields at all: told how to call again, it does
        raise ToolError(
            "Nom du dossier manquant : rappelle Experio_parametrer_dossier avec dossier_id 0 et les informations "
            "du client remplies (name, ice, if_number, folder_type)."
        )
    words = "\n".join(client_messages(session_id, client_message))
    ice, if_number, not_taken = client_ids(ice, if_number, words, client_message)
    require_folder_type(folder_type)
    reused = created_here(session_id, name)
    if reused:
        return ok(
            f"Dossier `{reused['name']}` deja cree dans cette conversation : il est repris.",
            dossier_id=reused["id"],
            reused=True,
        )
    if drawn_from_a_file(session_id, name, words):  # a file's name is not a dossier's: this conversation's, or ask
        with cabinet_db() as db:
            made = [dossier_id for (dossier_id,) in db.execute(Q.dossiers_created_in_session, (session_id,))]
        if len(made) == 1:
            dossier = odoo_call("res.company", "read", made, fields=["name"])[0]
            return ok(
                f"`{name}` est le nom d'un fichier, pas d'un dossier : le dossier `{dossier['name']}`, créé dans cette "
                "conversation, est repris.",
                dossier_id=dossier["id"],
                reused=True,
            )
        return question(f"`{name}` est le nom d'un fichier reçu. Quel est le nom du dossier à créer ?")
    refusal = duplicate(session_id, client_confirmed, name, ice, if_number)
    if refusal:
        return refusal
    values = {"name": name, "folder_type": folder_type, "enabled": True}  # enabled, as Experio's dossier import sets it
    if skip_ice_if_check:
        if not confirmed(session_id, client_confirmed, "sans cette verification", name):
            return question(SKIP_QUESTION.format(what=f"la creation du dossier `{name}`"))
        values["ignore_ice_if_verification"] = True
    for field, value in (("ice", ice), ("vat", if_number)):
        if value and value.strip():
            values[field] = value.strip()
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier `{name}` serait cree.{not_taken}")
    with refused(f"create_dossier {name}", f"Experio refuse de creer le dossier `{name}` : {{reason}}"):
        dossier_id = odoo_json("res.company", "create", values, context=main_dossier_context())
        attach_user(None, dossier_id)  # Tarik works in it from here on
    unchecked = " ICE et IF non verifies (verification ignoree, a la demande du client)." if skip_ice_if_check else ""
    return ok(f"Dossier `{name}` cree dans Experio.{unchecked}{not_taken}", dossier_id=dossier_id)


# --- Changing a dossier ---


def given(arguments):
    """Return the tool ``arguments`` ({key: value}) actually given (not None, not blank), as stripped text; a
    capital given as a number stays one (as text, 1.005 could read back as 1.005 or 1005)."""
    return {
        key: value if key == "capital" and isinstance(value, (int, float)) else str(value).strip()
        for key, value in arguments.items()
        if value is not None and str(value).strip()
    }


def new_value(key, value):
    """Return the value Experio is given for ``key``: a capital as a number, a name with single spaces, any
    other value unchanged."""
    if key == "capital":
        return as_number(value)
    if key == "name":
        return " ".join(value.split())
    return value


def changed(key, old, new):
    """True when ``new`` differs from the dossier's ``old`` value for ``key``; a capital is compared as a
    number."""
    if (old or "") == new:
        return False
    return not (key == "capital" and float(old or 0) == new)


def changes_to(dossier_id, asked):
    """Return (current, changes) for the ``asked`` fields ({tool argument: value}): the dossier's current values
    ({Experio field: value}) and the new values that differ from them ({Experio field: new value})."""
    fields = [EDITABLE[key][0] for key in asked]
    current = odoo_call("res.company", "read", [dossier_id], fields=fields)[0] if fields else {}
    changes = {}
    for key, value in asked.items():
        field = EDITABLE[key][0]
        new = new_value(key, value)
        if changed(key, current.get(field), new):
            changes[field] = new
    return current, changes


def conflict(dossier_id, dossier, changes):
    """Return a stop result when ``changes`` would give dossier ``dossier`` (its name) another dossier's real ICE,
    IF or name; else None."""
    for field, value in real_ids((field, changes.get(field)) for field in ("ice", "vat")):
        other = having([(field, value)], ("id", "!=", dossier_id))
        if other:
            label = "ICE" if field == "ice" else "IF"
            return stop(
                f"Dossier `{dossier}` non modifie : le dossier `{other[0]['name']}` a deja cet {label} ({value})."
            )
    new_name = changes.get("name")
    if new_name and any(normalized(d["name"]) == normalized(new_name) and d["id"] != dossier_id for d in dossiers()):
        return stop(f"Dossier `{dossier}` non modifie : un autre dossier s'appelle deja `{new_name}`.")
    return None


@step_tool
def update_dossier(
    dossier_id: int,
    name: str = "",
    folder_type: str = "",
    ice: str | int = "",
    if_number: str | int = "",
    rc: str | int = "",
    patente: str | int = "",
    tsc: str | int = "",
    capital: str | int | float = "",
    cnss: str | int = "",
    street: str = "",
    street2: str = "",
    city: str = "",
    zip: str | int = "",
    skip_ice_if_check: bool = False,
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Change fields of an existing dossier, as its Experio form does: only the fields given (empty: unchanged),
    and only to values the client wrote themselves (``client_message`` and their earlier messages in
    ``session_id``); the others are named in the reply as not changed.

    Experio checks the ICE and IF against the registry (exp.partner.base _check_ice_if_verification);
    ``skip_ice_if_check`` ticks "Ignore ICE/IF Verification", asked to the client first. ``client_confirmed`` is
    the agent's reading of a clear yes to that question and confirms it only.

    Returns a stop result when nothing changes or when the new ICE, IF or name is another dossier's; a question
    result to confirm skipping the check; an ok result listing each change (in test mode, without writing).

    Raises:
        ToolError: no field given, or an unknown ``folder_type``.
    """
    client_message = own_words(client_message)
    dossier = require_dossier(dossier_id)
    asked = given(
        {
            "name": name,
            "folder_type": folder_type,
            "ice": ice,
            "if_number": if_number,
            "rc": rc,
            "patente": patente,
            "tsc": tsc,
            "capital": capital,
            "cnss": cnss,
            "street": street,
            "street2": street2,
            "city": city,
            "zip": zip,
        }
    )
    if not asked:  # MiniMax sometimes calls with no fields: told how to call again, it does
        raise ToolError(
            "Aucun champ a modifier : rappelle Experio_modifier_dossier avec le dossier_id et les seuls champs "
            "que le client veut changer, remplis avec ses valeurs."
        )
    words = "\n".join(client_messages(session_id, client_message))
    for key, pattern in (("ice", ICE_WRITTEN), ("if_number", IF_WRITTEN)):
        if key in asked:
            asked[key] = written_id(asked[key], pattern, words)
    if asked.get("folder_type"):
        require_folder_type(asked["folder_type"])
    written = {key: value for key, value in asked.items() if client_wrote(key, value, words)}
    not_written = [EDITABLE[key][1] for key in asked if key not in written]
    unchanged = f" Non modifie, le client ne l'a pas ecrit : {', '.join(not_written)}." if not_written else ""
    current, changes = changes_to(dossier_id, written)
    if not changes:
        return stop(f"Dossier `{dossier}` : rien a modifier.{unchanged}")
    refusal = conflict(dossier_id, dossier, changes)
    if refusal:
        return refusal
    if skip_ice_if_check:
        if not confirmed(session_id, client_confirmed, "sans cette verification", dossier):
            return question(SKIP_QUESTION.format(what=f"la modification du dossier `{dossier}`"))
        changes["ignore_ice_if_verification"] = True
    each = []
    for key in written:
        field, label = EDITABLE[key]
        if field in changes:
            each.append(f"{label} : {current.get(field) or 'vide'} -> {changes[field]}")
    shown = " ; ".join(each)
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier `{dossier}` serait modifie : {shown}.{unchanged}")
    with refused(f"update_dossier {dossier_id}", f"Experio refuse de modifier le dossier `{dossier}` : {{reason}}"):
        odoo_json("res.company", "write", [dossier_id], changes)
    return ok(f"Dossier `{dossier}` modifie : {shown}.{unchanged}")
