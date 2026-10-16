"""A dossier's identity, as the top of Experio's dossier form shows it (name, type, ICE, IF, RC...):
creating a dossier, and changing those fields of an existing one. Only values the client wrote
themselves are used; an ICE, IF or name another dossier has is refused."""

import re
from typing import Final

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, question, server, stop, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.config import DOSSIER_DOMAIN, SIMILAR_NAME, TEST_MODE
from tarik.core.conversation import client_messages, own_words, yes_no
from tarik.core.experio import dry_run, odoo_call, odoo_json, refused, require_dossier
from tarik.core.numbers import as_number, written_numbers
from tarik.core.text import normalized, said, similarity
from tarik.dossiers.search import dossiers

FOLDER_TYPES = {"company": "societe", "person": "personne physique", "cpu": "CPU", "st": "ST"}
PLACEHOLDER_IDS = {"1111", "2222"}  # Experio's own "not known yet" ICE/IF, shared by many dossiers

SKIP_QUESTION = (
    "Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous {what} sans cette "
    "verification ? oui / non"
)
SIMILAR_QUESTION = (
    "Un dossier au nom tres proche existe deja ({names}). Faut-il quand meme creer un nouveau dossier {name} ? "
    "oui / non"
)

# 15 digits, not inside a longer number: an ICE.
ICE_WRITTEN: Final = re.compile(r"(?<!\d)\d{15}(?!\d)")

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
    """A regex for the number written right after ``label`` (a regex): the label as a whole word, LINK, then
    its digits (captured), not followed by another digit."""
    return re.compile(rf"\b{label}\b{LINK}(\d+)(?!\d)", re.IGNORECASE)


# The identifiers a client writes after their label: their number counts for that field only ("IF 12345678" is no RC).
# An ICE is told apart by its 15 digits alone.
IF_WRITTEN: Final = labelled(r"I\.?F\.?")
LABELLED = {"ice": ICE_WRITTEN, "if_number": IF_WRITTEN, "rc": labelled(r"R\.?C\.?")}
# "societe" alone is not a type: "l'adresse de ma societe" changes no dossier into a company.
TYPE_WORDS = {
    "person": ("personne physique", "physique"),
    "company": ("personne morale", "morale"),
    "cpu": ("cpu",),
    "st": ("sole trader", "commercant", "entreprise individuelle"),
}


# --- What the client wrote ---


def joined(text, separators=" .  "):
    """``text`` with the ``separators`` inside a number removed: "IF 71 234 561" -> "IF 71234561"."""
    return re.sub(rf"(?<=\d)[{separators}](?=\d)", "", text or "")  # one separator between two digits


def digits(text):
    """The numbers in a text, their spaces, dots and commas between digits removed (100 000 -> 100000)."""
    return re.findall(r"\d+", joined(text, " .,  "))  # each run of digits


def as_written(value, written, current=None):
    """An ICE or IF as the client wrote it (``written``: the numbers found in their messages). The
    model may send it as a number, which loses its leading zeros, or leave it out: the client's own
    number is used whenever it is the same number, or when it is the only one in ``current`` (their
    latest message, by default ``written``)."""
    current = written if current is None else current
    value = str(value if value is not None else "").strip()
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    if value.isdigit():
        same = [number for number in written if number.lstrip("0") == value.lstrip("0")]
        return same[0] if same else value
    if not value and len(set(current)) == 1:
        return current[0]
    return value


def written_id(value, pattern, words, latest=None):
    """``value`` (an ICE or IF, ``pattern`` finding it) as_written in ``words`` (all the client's messages) and
    ``latest`` (their last message; None: ``words``)."""
    current = None if latest is None else pattern.findall(joined(latest))
    return as_written(value, pattern.findall(joined(words)), current)


def client_wrote(key, value, words):
    """Did the client write this new value themselves? A model may not choose nor "correct" one.
    An identifier is one of the numbers they wrote, digit for digit; the capital, one of their amounts,
    its decimals kept (100 000,50); a text, whole words of theirs."""
    if key == "capital":
        try:
            amount = as_number(value)
        except ValueError:
            return False
        return any(abs(amount - written) < 0.005 for written in written_numbers(words))
    if key in IDENTIFIERS:
        number = re.sub(r"\.0+$", "", str(value)).replace(" ", "")  # a trailing ".0" a float left
        found = LABELLED[key].findall(joined(words)) if key in LABELLED else digits(words)
        return number.isdigit() and number in found
    if key == "folder_type":
        return any(said(word, normalized(words)) for word in TYPE_WORDS.get(value, ()))
    return said(normalized(value), normalized(words))


def confirmed(session_id, client_message, *parts):
    """The client's "oui" answers Tarik's last question in this conversation, and that question holds every one of
    ``parts`` (its kind and the dossier's name): a yes to another question, or about another dossier, confirms
    nothing. No guess from their wording, and the model can't decide it."""
    with cabinet_db() as db:
        last = db.execute(
            "select message from tarik_results where session_id = %s and status = 'question' order by id desc limit 1",
            (session_id,),
        ).fetchone()
    return (
        bool(last) and all(normalized(part) in normalized(last[0]) for part in parts) and yes_no(client_message) is True
    )


# --- Other dossiers ---


def real_ids(pairs):
    """The (field, value) of ``pairs`` holding a real ICE or IF: stripped, neither empty nor a placeholder."""
    return [
        (field, value.strip())
        for field, value in pairs
        if value and value.strip() and value.strip() not in PLACEHOLDER_IDS
    ]


def having(keys, *extra):
    """The dossiers ([{id, name}]) with any of ``keys`` ([(field, value)], at least one), ``extra`` domain terms
    added."""
    either = ["|"] * (len(keys) - 1)
    domain = [*DOSSIER_DOMAIN, *either, *[(field, "=", value) for field, value in keys], *extra]
    return odoo_call("res.company", "search_read", domain, fields=["name"])


def lookalikes(name, ice, if_number):
    """Existing dossiers a new one may duplicate: the same ICE or IF, or a very similar name."""
    keys = real_ids((("ice", ice), ("vat", if_number)))
    same = having(keys) if keys else []
    return same, [dossier for dossier in dossiers() if similarity(name, dossier["name"]) >= SIMILAR_NAME]


def require_folder_type(folder_type):
    """ToolError unless ``folder_type`` is one of FOLDER_TYPES."""
    if folder_type not in FOLDER_TYPES:
        raise ToolError(f"Type de dossier inconnu : {', '.join(FOLDER_TYPES)}.")


# --- Creating a dossier ---


def client_ids(ice, if_number, words, client_message):
    """The ICE and IF as the client wrote them, "" for one they did not write, and the note naming those
    left out: (ice, if_number, note)."""
    ice = written_id(ice, ICE_WRITTEN, words, client_message)
    if_number = written_id(if_number, IF_WRITTEN, words, client_message)
    given = (("ice", "ICE", ice), ("if_number", "IF", if_number))
    left_out = [label for key, label, value in given if value and not client_wrote(key, value, words)]
    ice, if_number = ("" if "ICE" in left_out else ice), ("" if "IF" in left_out else if_number)
    note = f" Non repris, le client ne l'a pas ecrit : {', '.join(left_out)}." if left_out else ""
    return ice, if_number, note


def created_here(session_id, name):
    """The dossier ({id, name}) named ``name`` this conversation already created, or None."""
    with cabinet_db() as db:
        created = [
            dossier_id
            for (dossier_id,) in db.execute(
                "select distinct dossier_id from tarik_results where session_id = %s and "
                "step = 'create_dossier' and status = 'ok' and dossier_id is not null",
                (session_id,),
            )
        ]
    for dossier in odoo_call("res.company", "read", created, fields=["name"]) if created else []:
        if normalized(dossier["name"]) == normalized(name):
            return dossier
    return None


def duplicate(session_id, client_message, name, ice, if_number):
    """A stop when another dossier has this ICE or IF, a question while one has a very similar name the client
    did not confirm; None when the dossier may be created."""
    same, similar = lookalikes(name, ice, if_number)
    if same:
        return stop(
            f"Dossier {name} non cree : un dossier existe deja avec cet ICE ou cet IF "
            f"({', '.join(d['name'] for d in same)})."
        )
    # The skip question comes only once this dossier's similar name was confirmed: a yes to it confirms both.
    if similar and not (
        confirmed(session_id, client_message, "nom tres proche", name)
        or confirmed(session_id, client_message, "sans cette verification", name)
    ):
        return question(SIMILAR_QUESTION.format(names=", ".join(d["name"] for d in similar), name=name))
    return None


@server.tool()
def create_dossier(
    name: str = "",
    ice: str | int = "",
    if_number: str | int = "",
    folder_type: str = "company",
    skip_ice_if_check: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Create a new dossier in Experio, as the Folders screen does. ``ice`` and
    ``if_number`` are the client's ICE and IF, ``folder_type`` company (default),
    person, cpu or st. Experio checks the name is free and the ICE/IF against the
    official registry; ``skip_ice_if_check`` ticks the dossier form's "Ignore ICE/IF
    Verification", once the client confirmed it (a question first). The ICE and IF are kept only
    when the client wrote them (``client_message`` and their earlier messages).
    A dossier of that name this conversation (``session_id``) already created is reused:
    MiniMax sometimes asks for it again to import its entries. Nothing is created when a
    dossier has the same ICE or IF; a very similar name is a question to the client first."""
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
            f"Dossier {reused['name']} deja cree dans cette conversation : il est repris.",
            dossier_id=reused["id"],
            reused=True,
        )
    refusal = duplicate(session_id, client_message, name, ice, if_number)
    if refusal:
        return refusal
    values = {"name": name, "folder_type": folder_type, "enabled": True}  # enabled, as Experio's dossier import sets it
    if skip_ice_if_check:
        if not confirmed(session_id, client_message, "sans cette verification", name):
            return question(SKIP_QUESTION.format(what=f"la creation du dossier {name}"))
        values["ignore_ice_if_verification"] = True
    values.update({key: value.strip() for key, value in (("ice", ice), ("vat", if_number)) if value and value.strip()})
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier {name} serait cree.{not_taken}")
    with refused(f"create_dossier {name}", f"Experio refuse de creer le dossier {name} : {{reason}}"):
        dossier_id = odoo_json("res.company", "create", values)
    unchecked = " ICE et IF non verifies (verification ignoree, a la demande du client)." if skip_ice_if_check else ""
    return ok(f"Dossier {name} cree dans Experio.{unchecked}{not_taken}", dossier_id=dossier_id)


# --- Changing a dossier ---


def given(arguments):
    """The tool ``arguments`` ({key: value}) actually given, stripped text; a capital given as a number stays one
    (as text, 1.005 would read 1.005 or 1005 again)."""
    return {
        key: value if key == "capital" and isinstance(value, (int, float)) else str(value).strip()
        for key, value in arguments.items()
        if value is not None and str(value).strip()
    }


def changes_to(dossier_id, asked):
    """The dossier's current values of the ``asked`` fields ({Experio field: value}) and the changes
    ({Experio field: new value}) that differ from them."""
    fields = [EDITABLE[key][0] for key in asked]
    current = odoo_call("res.company", "read", [dossier_id], fields=fields)[0] if fields else {}
    changes = {}
    for key, value in asked.items():
        field = EDITABLE[key][0]
        new = as_number(value) if key == "capital" else (" ".join(value.split()) if key == "name" else value)
        if (current.get(field) or "") != new and not (key == "capital" and float(current.get(field) or 0) == new):
            changes[field] = new
    return current, changes


def conflict(dossier_id, dossier, changes):
    """A stop when ``changes`` would give dossier ``dossier`` another dossier's ICE, IF or name; else None."""
    for field, value in real_ids((field, changes.get(field)) for field in ("ice", "vat")):
        other = having([(field, value)], ("id", "!=", dossier_id))
        if other:
            label = "ICE" if field == "ice" else "IF"
            return stop(f"Dossier {dossier} non modifie : le dossier {other[0]['name']} a deja cet {label} ({value}).")
    new_name = changes.get("name")
    if new_name and any(normalized(d["name"]) == normalized(new_name) and d["id"] != dossier_id for d in dossiers()):
        return stop(f"Dossier {dossier} non modifie : un autre dossier s'appelle deja {new_name}.")
    return None


@server.tool()
@with_ids
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
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Change fields of an existing dossier, as its Experio form does: only those given (empty: unchanged),
    and only to values the client wrote themselves (``client_message`` and their earlier messages in
    ``session_id``). An ICE or IF another dossier has, or a name another dossier has, is refused; Experio
    checks the ICE and IF against the registry, skipped only once the client confirmed it (a question first)."""
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
        return stop(f"Dossier {dossier} : rien a modifier.{unchanged}")
    refusal = conflict(dossier_id, dossier, changes)
    if refusal:
        return refusal
    if skip_ice_if_check:
        if not confirmed(session_id, client_message, "sans cette verification", dossier):
            return question(SKIP_QUESTION.format(what=f"la modification du dossier {dossier}"))
        changes["ignore_ice_if_verification"] = True
    shown = " ; ".join(
        f"{label} : {current.get(field) or 'vide'} -> {changes[field]}"
        for field, label in (EDITABLE[key] for key in written)
        if field in changes
    )
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier {dossier} serait modifie : {shown}.{unchanged}")
    with refused(f"update_dossier {dossier_id}", f"Experio refuse de modifier le dossier {dossier} : {{reason}}"):
        odoo_json("res.company", "write", [dossier_id], changes)
    return ok(f"Dossier {dossier} modifie : {shown}.{unchanged}")
