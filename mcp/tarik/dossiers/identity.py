"""A dossier's identity, as the top of Experio's dossier form shows it (name, type, ICE, IF, RC...):
creating a dossier, and changing those fields of an existing one. Only values the client wrote
themselves are used; an ICE, IF or name another dossier has is refused."""

import re

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, question, server, stop, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.conversation import client_messages, remember_client_message, yes_no
from tarik.core.experio import DOSSIER_DOMAIN, TEST_MODE, dry_run, experio_refusal, odoo_call, odoo_json, require_dossier
from tarik.core.numbers import as_number, written_numbers
from tarik.dossiers.search import dossiers, normalized, similarity

FOLDER_TYPES = {"company": "societe", "person": "personne physique", "cpu": "CPU", "st": "ST"}


PLACEHOLDER_IDS = {"1111", "2222"}  # Experio's own "not known yet" ICE/IF, shared by many dossiers
SIMILAR_NAME = 0.9  # dossiers.similarity: "TARIK FEC XLSX 2" against "TARIK FEC XLSX" is 0.93


def lookalikes(name, ice, if_number):
    """Existing dossiers a new one may duplicate: the same ICE or IF, or a very similar name."""
    keys = [(field, value.strip()) for field, value in (("ice", ice), ("vat", if_number))
            if value and value.strip() and value.strip() not in PLACEHOLDER_IDS]
    same = odoo_call("res.company", "search_read", [*DOSSIER_DOMAIN, *["|"] * (len(keys) - 1),
                                                    *[(field, "=", value) for field, value in keys]], fields=["name"]) if keys else []
    return same, [dossier for dossier in dossiers() if similarity(name, dossier["name"]) >= SIMILAR_NAME]


def similar_confirmed(session_id, name, client_message):
    """The client said yes to the question about a dossier with a very similar name, asked in this conversation."""
    with cabinet_db() as db:
        asked = [message for (message,) in db.execute("select message from tarik_results where session_id = %s and "
                                                       "step = 'create_dossier' and status = 'question'", (session_id,))]
    return yes_no(client_message) is True and any(normalized(name) in normalized(message) for message in asked)


SKIP_QUESTION = ("Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous {what} sans cette "
                 "verification ? oui / non")


def skip_confirmed(session_id, step, client_message):
    """Skipping Experio's ICE/IF check needs the client's yes to Tarik's own question about it, the last one
    this step asked in the conversation: no guess from their wording, and the model can't decide it."""
    with cabinet_db() as db:
        last = db.execute("select message from tarik_results where session_id = %s and step = %s and status = 'question' "
                          "order by id desc limit 1", (session_id, step)).fetchone()
    return bool(last) and "sans cette verification" in last[0] and yes_no(client_message) is True


ICE_WRITTEN = re.compile(r"(?<!\d)\d{15}(?!\d)")  # an ICE: 15 digits
IF_WRITTEN = re.compile(r"\bI\.?F\.?\s*(?:n\s*[°o]\s*)?[:=]?\s*(\d{4,12})(?!\d)", re.IGNORECASE)


def as_written(value, written):
    """An ICE or IF as the client wrote it (``written``: the numbers found in their message). The
    model may send it as a number, which loses its leading zeros, or leave it out: the client's own
    number is used whenever it is the same number, or when it is the only one they wrote."""
    value = str(value if value is not None else "").strip()
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    if value.isdigit():
        same = [number for number in written if number.lstrip("0") == value.lstrip("0")]
        return same[0] if same else value
    if not value and len(set(written)) == 1:
        return written[0]
    return value


def require_folder_type(folder_type):
    if folder_type not in FOLDER_TYPES:
        raise ToolError(f"Type de dossier inconnu : {', '.join(FOLDER_TYPES)}.")


@server.tool()
def create_dossier(name: str = "", ice: str | int = "", if_number: str | int = "", folder_type: str = "company",
                   skip_ice_if_check: bool = False, client_message: str = "", session_id: str = "") -> dict:
    """Create a new dossier in Experio, as the Folders screen does. ``ice`` and
    ``if_number`` are the client's ICE and IF, ``folder_type`` company (default),
    person, cpu or st. Experio checks the name is free and the ICE/IF against the
    official registry; ``skip_ice_if_check`` ticks the dossier form's "Ignore ICE/IF
    Verification", once the client confirmed it (a question first). The ICE and IF are kept only
    when the client wrote them (``client_message`` and their earlier messages).
    A dossier of that name this conversation (``session_id``) already created is reused:
    MiniMax sometimes asks for it again to import its entries. Nothing is created when a
    dossier has the same ICE or IF; a very similar name is a question to the client first."""
    remember_client_message(session_id, client_message)
    name = " ".join((name or "").split())
    if not name:  # MiniMax sometimes calls with no fields at all: told how to call again, it does
        raise ToolError("Nom du dossier manquant : rappelle Experio_parametrer_dossier avec dossier_id 0 et les informations "
                        "du client remplies (name, ice, if_number, folder_type).")
    ice = as_written(ice, ICE_WRITTEN.findall(client_message or ""))
    if_number = as_written(if_number, IF_WRITTEN.findall(client_message or ""))
    words = "\n".join(client_messages(session_id, client_message))
    left_out = [label for key, label, value in (("ice", "ICE", ice), ("if_number", "IF", if_number))
                if value and not client_wrote(key, value, words)]
    ice, if_number = ("" if "ICE" in left_out else ice), ("" if "IF" in left_out else if_number)
    not_taken = f" Non repris, le client ne l'a pas ecrit : {', '.join(left_out)}." if left_out else ""
    require_folder_type(folder_type)
    values = {"name": name, "folder_type": folder_type, "enabled": True}  # enabled, as Experio's dossier import sets it
    with cabinet_db() as db:
        created = [i for (i,) in db.execute("select distinct dossier_id from tarik_results where session_id = %s and "
                                            "step = 'create_dossier' and status = 'ok' and dossier_id is not null", (session_id,))]
    for dossier in odoo_call("res.company", "read", created, fields=["name"]) if created else []:
        if normalized(dossier["name"]) == normalized(name):
            return ok(f"Dossier {dossier['name']} deja cree dans cette conversation : il est repris.",
                      dossier_id=dossier["id"], reused=True)
    same, similar = lookalikes(name, ice, if_number)
    if same:
        return stop(f"Dossier {name} non cree : un dossier existe deja avec cet ICE ou cet IF ({', '.join(d['name'] for d in same)}).")
    if similar and not similar_confirmed(session_id, name, client_message):
        return question(f"Un dossier au nom tres proche existe deja ({', '.join(d['name'] for d in similar)}). "
                        f"Faut-il quand meme creer un nouveau dossier {name} ? oui / non")
    if skip_ice_if_check:
        if not skip_confirmed(session_id, "create_dossier", client_message):
            return question(SKIP_QUESTION.format(what=f"la creation du dossier {name}"))
        values["ignore_ice_if_verification"] = True
    values.update({key: value.strip() for key, value in (("ice", ice), ("vat", if_number)) if value and value.strip()})
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier {name} serait cree.{not_taken}")
    try:
        dossier_id = odoo_json("res.company", "create", values)
    except Exception as error:  # not refused(): Experio's own reason is told to the client
        print(f"create_dossier {name}: {error}", flush=True)
        raise ToolError(f"Experio refuse de creer le dossier {name} : {experio_refusal(error)}") from None
    unchecked = " ICE et IF non verifies (verification ignoree, a la demande du client)." if skip_ice_if_check else ""
    return ok(f"Dossier {name} cree dans Experio.{unchecked}{not_taken}", dossier_id=dossier_id)


# What a client may change in a dossier: the fields of Experio's dossier form, before its tabs (the
# currency and the logo aside). Tool argument -> Experio field, and how Tarik names it to the client.
EDITABLE = {"name": ("name", "nom"), "folder_type": ("folder_type", "type"), "ice": ("ice", "ICE"), "if_number": ("vat", "IF"),
            "rc": ("rc", "RC"), "patente": ("pat", "patente"), "tsc": ("tsc", "T.S.C"), "capital": ("capital", "capital"),
            "cnss": ("cnss_number", "CNSS"), "street": ("street", "adresse"), "street2": ("street2", "complement d'adresse"),
            "city": ("city", "ville"), "zip": ("zip", "code postal")}
IDENTIFIERS = {"ice", "if_number", "rc", "patente", "tsc", "cnss", "zip"}  # checked digit for digit
TYPE_WORDS = {"person": ("personne physique", "physique"), "company": ("societe", "personne morale", "morale"),
              "cpu": ("cpu",), "st": ("sole trader", "commercant", "entreprise individuelle")}


def digits(text):
    """The numbers in a text, their spaces, dots and commas between digits removed (100 000 -> 100000)."""
    return re.findall(r"\d+", re.sub(r"(?<=\d)[ .,\u202f\u00a0](?=\d)", "", text or ""))


def said(phrase, words):
    """``phrase`` is in ``words`` as whole words: "alpha" is not in "alphanumerique"."""
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", words) is not None


def client_wrote(key, value, words):
    """Did the client write this new value themselves? A model may not choose nor "correct" one.
    An identifier is one of the numbers they wrote, digit for digit; the capital, one of their amounts,
    its decimals kept (100 000,50); a text, whole words of theirs."""
    if key == "capital":
        try:
            amount = as_number(str(value))
        except ValueError:
            return False
        return any(abs(amount - written) < 0.005 for written in written_numbers(words))
    if key in IDENTIFIERS:
        number = re.sub(r"\.0+$", "", str(value)).replace(" ", "")
        return number.isdigit() and number in digits(words)
    if key == "folder_type":
        return any(said(word, normalized(words)) for word in TYPE_WORDS.get(value, ()))
    return said(normalized(value), normalized(words))


@server.tool()
@with_ids
def update_dossier(dossier_id: int, name: str = "", folder_type: str = "", ice: str | int = "", if_number: str | int = "",
                   rc: str | int = "", patente: str | int = "", tsc: str | int = "", capital: str | int | float = "",
                   cnss: str | int = "", street: str = "", street2: str = "", city: str = "", zip: str | int = "",
                   skip_ice_if_check: bool = False, client_message: str = "", session_id: str = "") -> dict:
    """Change fields of an existing dossier, as its Experio form does: only those given (empty: unchanged),
    and only to values the client wrote themselves (``client_message`` and their earlier messages in
    ``session_id``). An ICE or IF another dossier has, or a name another dossier has, is refused; Experio
    checks the ICE and IF against the registry, skipped only once the client confirmed it (a question first)."""
    dossier = require_dossier(dossier_id)
    asked = {key: str(value).strip() for key, value in (
        ("name", name), ("folder_type", folder_type), ("ice", ice), ("if_number", if_number), ("rc", rc), ("patente", patente),
        ("tsc", tsc), ("capital", capital), ("cnss", cnss), ("street", street), ("street2", street2), ("city", city), ("zip", zip))
        if value is not None and str(value).strip()}
    if not asked:  # MiniMax sometimes calls with no fields: told how to call again, it does
        raise ToolError("Aucun champ a modifier : rappelle Experio_modifier_dossier avec le dossier_id et les seuls champs "
                        "que le client veut changer, remplis avec ses valeurs.")
    words = "\n".join(client_messages(session_id, client_message))
    for key, found in (("ice", ICE_WRITTEN), ("if_number", IF_WRITTEN)):
        if key in asked:
            asked[key] = as_written(asked[key], found.findall(words))
    if asked.get("folder_type"):
        require_folder_type(asked["folder_type"])
    not_written = [EDITABLE[key][1] for key, value in asked.items() if not client_wrote(key, value, words)]
    asked = {key: value for key, value in asked.items() if client_wrote(key, value, words)}
    unchanged = f" Non modifie, le client ne l'a pas ecrit : {', '.join(not_written)}." if not_written else ""
    fields = [EDITABLE[key][0] for key in asked]
    current = odoo_call("res.company", "read", [dossier_id], fields=fields)[0] if fields else {}
    changes = {}
    for key, value in asked.items():
        field = EDITABLE[key][0]
        new = as_number(value) if key == "capital" else (" ".join(value.split()) if key == "name" else value)
        if (current.get(field) or "") != new and not (key == "capital" and float(current.get(field) or 0) == new):
            changes[field] = new
    if not changes:
        return stop(f"Dossier {dossier} : rien a modifier.{unchanged}")
    for field in ("ice", "vat"):
        if changes.get(field) and changes[field] not in PLACEHOLDER_IDS:
            other = odoo_call("res.company", "search_read", [*DOSSIER_DOMAIN, (field, "=", changes[field]), ("id", "!=", dossier_id)], fields=["name"])
            if other:
                label = "ICE" if field == "ice" else "IF"
                return stop(f"Dossier {dossier} non modifie : le dossier {other[0]['name']} a deja cet {label} ({changes[field]}).")
    if "name" in changes and any(normalized(d["name"]) == normalized(changes["name"]) and d["id"] != dossier_id for d in dossiers()):
        return stop(f"Dossier {dossier} non modifie : un autre dossier s'appelle deja {changes['name']}.")
    if skip_ice_if_check:
        if not skip_confirmed(session_id, "update_dossier", client_message):
            return question(SKIP_QUESTION.format(what=f"la modification du dossier {dossier}"))
        changes["ignore_ice_if_verification"] = True
    shown = " ; ".join(f"{EDITABLE[key][1]} : {current.get(EDITABLE[key][0]) or 'vide'} -> {changes[EDITABLE[key][0]]}"
                       for key in asked if EDITABLE[key][0] in changes)
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier {dossier} serait modifie : {shown}.{unchanged}")
    try:
        odoo_json("res.company", "write", [dossier_id], changes)
    except Exception as error:  # not refused(): Experio's own reason is told to the client
        print(f"update_dossier {dossier_id}: {error}", flush=True)
        raise ToolError(f"Experio refuse de modifier le dossier {dossier} : {experio_refusal(error)}") from None
    return ok(f"Dossier {dossier} modifie : {shown}.{unchanged}")


