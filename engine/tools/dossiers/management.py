"""Manage dossiers and who works on them, as Experio's Folders screen does: create the dossiers listed in a
spreadsheet, create a collaborateur or a client and give them a dossier, archive a dossier.

Each tool reads the values back and writes only once the client said yes to that read-back (identity.confirmed):
a res.company or a res.users cannot be deleted from here, and only the client can catch a misread digit.
"""

import csv
import io

from engine.tools.core.app import ToolError, ok, question, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import dry_run, experio_refusal, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.words import ICE_WRITTEN, normalized, said
from engine.tools.dossiers.identity import FOLDER_TYPES, PLACEHOLDER_IDS, confirmed, having, real_ids
from engine.tools.dossiers.people import attach_user, users_by_email
from engine.tools.dossiers.search import dossiers
from engine.tools.dossiers.setup import separator_of, table_text, taken
from engine.tools.invoices.intake import file_row

DECLARATION_MODES = {"monthly": "mensuelle", "quarterly": "trimestrielle", "out_of_field": "hors champ"}
MODE_WORDS = {
    "mensuelle": "monthly",
    "mensuel": "monthly",
    "trimestrielle": "quarterly",
    "trimestriel": "quarterly",
    "hors champ": "out_of_field",
}
# A spreadsheet's Type column, where "societe" is a type (a message's words are not: identity.TYPE_WORDS).
TYPE_WORDS = {
    "personne physique": "person",
    "physique": "person",
    "personne morale": "company",
    "morale": "company",
    "societe": "company",
    "cpu": "cpu",
    "st": "st",
    "auto entrepreneur": "st",
    "autoentrepreneur": "st",
}
# The spreadsheet's columns, by the headers a cabinet writes (normalized). The first five are required.
COLUMNS = {
    "name": ("nom", "raison sociale", "nom du dossier", "dossier", "societe", "denomination"),
    "folder_type": ("type", "type de dossier", "forme juridique", "forme"),
    "ice": ("ice",),
    "vat": ("if", "identifiant fiscal", "id fiscal", "vat"),
    "declaration_mode": ("mode de declaration", "declaration", "declaration tva", "tva", "regime tva"),
    "country": ("pays", "country"),
    "city": ("ville", "city"),
    "zip": ("code postal", "cp", "zip"),
    "ras_tva": ("ras tva", "ras"),
}
REQUIRED = ("name", "folder_type", "ice", "vat", "declaration_mode")
LABELS = {"name": "Nom", "folder_type": "Type", "ice": "ICE", "vat": "IF", "declaration_mode": "Mode de declaration"}
YES_NO = {
    "oui": True,
    "o": True,
    "x": True,
    "1": True,
    "vrai": True,
    "non": False,
    "n": False,
    "0": False,
    "faux": False,
}
MAX_SHOWN = 15


# --- A list of dossiers from a spreadsheet ---


def header_columns(name, header):
    """Return the spreadsheet's columns ({index: key of COLUMNS}) from its ``header`` row. Raises ToolError, in
    French and naming the file ``name``, when a REQUIRED column is missing."""
    by_header = {normalized(alias): key for key, aliases in COLUMNS.items() for alias in aliases}
    columns = {i: by_header[normalized(h)] for i, h in enumerate(header) if normalized(h) in by_header}
    missing = [LABELS[key] for key in REQUIRED if key not in columns.values()]
    if missing:
        raise ToolError(
            f"`{name}` : il manque des colonnes dans la premiere ligne : {', '.join(missing)}. "
            "Les colonnes attendues sont : Nom, Type, ICE, IF, Mode de declaration (puis Pays, Ville, Code postal, "
            "RAS TVA, facultatives)."
        )
    return columns


def read_row(line, cells, columns):
    """Return one spreadsheet row (its ``cells``, at file line ``line``, ``columns`` from header_columns) as a
    dossier dict: type, mode and RAS TVA read as Experio's values (None when not understood), the ICE padded to
    15 digits, the words as given kept under <key>_given."""
    given = {key: (cells[i].strip() if i < len(cells) else "") for i, key in columns.items()}
    given = {key: given.get(key, "") for key in COLUMNS}
    return {
        "line": line,
        **given,
        "folder_type": TYPE_WORDS.get(normalized(given["folder_type"])),
        "declaration_mode": MODE_WORDS.get(normalized(given["declaration_mode"])),
        "ras_tva": YES_NO.get(normalized(given["ras_tva"])) if given["ras_tva"] else None,
        "ice": given["ice"].replace(" ", "").zfill(15) if given["ice"].strip().isdigit() else given["ice"],
        "vat": given["vat"].replace(" ", ""),
        "folder_type_given": given["folder_type"],
        "declaration_mode_given": given["declaration_mode"],
        "ras_tva_given": given["ras_tva"],
    }


def read_rows(name, mime, data):
    """Return the spreadsheet's dossiers as [{line, name, folder_type, ice, vat, declaration_mode, country, city,
    zip, ras_tva}] (the words as given kept under <key>_given). Raises ToolError, in French, when the file is not
    Excel or CSV, unreadable, empty, misses a required column or has no row."""
    excel = name.lower().endswith((".xlsx", ".xlsm"))
    if not (excel or name.lower().endswith((".csv", ".txt")) or (mime or "").startswith("text/")):
        raise ToolError(f"`{name}` : envoyez la liste des dossiers en Excel (.xlsx) ou en CSV.")
    with refused(f"import_dossiers {name}", f"`{name}` : fichier illisible.", ToolError):
        text = table_text(name, data)
    table = [row for row in csv.reader(io.StringIO(text), delimiter=separator_of(text)) if any(row)]
    if not table:
        raise ToolError(f"`{name}` : le fichier est vide.")
    header, body = table[0], table[1:]
    columns = header_columns(name, header)
    rows = [read_row(line, cells, columns) for line, cells in enumerate(body, start=2)]  # line 1 is the header
    if not rows:
        raise ToolError(f"`{name}` : aucune ligne de dossier sous l'en-tete.")
    return rows


def country_of(term):
    """Return the res.country ({id, name}) a cabinet means by ``term`` ("Maroc", "Morocco" or "MA"), matched by
    code then by French or English name; None when none matches."""
    for domain in ([("code", "=ilike", term)], [("name", "=ilike", term)]):
        for lang in ("fr_FR", "en_US"):
            found = odoo_call(
                "res.country", "search_read", domain, fields=["id", "name"], limit=1, context={"lang": lang}
            )
            if found:
                return found[0]
    return None


def field_problems(row, where):
    """Return what is wrong with the row's own values (type, ICE, IF, mode, country, RAS TVA), one French line
    each, prefixed by ``where``. Side effect: the country found is stored in ``row`` (country_id,
    country_name)."""
    problems = []
    if not row["folder_type"]:
        problems.append(f"{where} : type « {row['folder_type_given']} » inconnu ({', '.join(FOLDER_TYPES.values())})")
    if row["ice"] and not ICE_WRITTEN.fullmatch(row["ice"]):
        problems.append(f"{where} : ICE « {row['ice']} » incorrect (15 chiffres)")
    if row["vat"] and not row["vat"].isdigit():
        problems.append(f"{where} : IF « {row['vat']} » incorrect (chiffres uniquement)")
    if not row["declaration_mode"]:
        problems.append(
            f"{where} : mode de declaration « {row['declaration_mode_given']} » inconnu "
            f"({', '.join(DECLARATION_MODES.values())})"
        )
    if row["country"]:
        country = country_of(row["country"])
        if country:
            row["country_id"], row["country_name"] = country["id"], country["name"]
        else:
            problems.append(f"{where} : pays « {row['country']} » inconnu")
    if row["ras_tva_given"] and row["ras_tva"] is None:
        problems.append(f"{where} : RAS TVA « {row['ras_tva_given']} » incompris (oui ou non)")
    return problems


def problems_of(rows):
    """Return everything wrong with the rows, one French line per problem; [] when every row can be created.
    Duplicates are looked for inside the file (by name and ICE) and in Experio (by name and by real ICE or IF,
    as create_dossier does)."""
    problems, seen_names, seen_ices = [], {}, {}
    existing = {normalized(d["name"]): d["name"] for d in dossiers()}
    for row in rows:
        where = f"Ligne {row['line']}" + (f" (`{row['name']}`)" if row["name"] else "")
        if not row["name"]:
            problems.append(f"{where} : le nom est vide")
            continue
        problems += field_problems(row, where)
        key = normalized(row["name"])
        if key in seen_names:
            problems.append(f"{where} : en double avec la ligne {seen_names[key]}")
        seen_names.setdefault(key, row["line"])
        if key in existing:
            problems.append(f"{where} : le dossier `{existing[key]}` existe deja")
        if row["ice"] and row["ice"] not in PLACEHOLDER_IDS:
            if row["ice"] in seen_ices:
                problems.append(f"{where} : meme ICE que la ligne {seen_ices[row['ice']]}")
            seen_ices.setdefault(row["ice"], row["line"])
        ids = real_ids((("ice", row["ice"]), ("vat", row["vat"])))
        same = having(ids) if ids else []
        if same:
            problems.append(f"{where} : l'ICE ou l'IF est deja celui du dossier `{same[0]['name']}`")
    return problems


def row_recap(row):
    """Return one dossier of the file as a French line the client reads back."""
    parts = [
        FOLDER_TYPES[row["folder_type"]],
        f"ICE {row['ice'] or 'non renseigne'}",
        f"IF {row['vat'] or 'non renseigne'}",
        DECLARATION_MODES[row["declaration_mode"]],
    ]
    town = " ".join(x for x in (row["zip"], row["city"]) if x)
    place = ", ".join(x for x in (town, row.get("country_name")) if x)
    if place:
        parts.append(place)
    if row["ras_tva"] is not None:
        parts.append("RAS TVA " + ("oui" if row["ras_tva"] else "non"))
    return f"- `{row['name']}` : " + ", ".join(parts)


def values_of(row):
    """Return a row as res.company create values, as the Folders screen creates a dossier (enabled, empty values
    left out)."""
    values = {
        "name": row["name"],
        "folder_type": row["folder_type"],
        "declaration_mode": row["declaration_mode"],
        "enabled": True,
    }
    values.update(
        {
            field: row[key]
            for key, field in (("ice", "ice"), ("vat", "vat"), ("city", "city"), ("zip", "zip"))
            if row[key]
        }
    )
    if row.get("country_id"):
        values["country_id"] = row["country_id"]
    if row["ras_tva"] is not None:
        values["declaration_ras_tva"] = row["ras_tva"]
    return values


@step_tool
def import_dossiers(file_id: int, client_confirmed: bool = False, session_id: str = "") -> dict:
    """Create the dossiers listed in a spreadsheet the client sent (columns Nom, Type, ICE, IF, Mode de
    declaration; Pays, Ville, Code postal, RAS TVA optional), and put Tarik's user on each.

    Returns:
        - a stop result listing the problems when any row has one: nothing is created, the client corrects
          the file;
        - a question result reading the list back until the client said yes (``client_confirmed``, the
          agent's reading of a clear yes to that question);
        - an ok result with how many were created (in test mode, would be). Experio checks each ICE and IF
          against the registry on creation: the rows it refuses are listed, the others are created, and the
          file is taken.

    Raises:
        ToolError: the file can't be read (read_rows).
    """
    with cabinet_db() as db:
        name, mime, data = file_row(db, file_id, "file_name", "mime_type", "data")
    rows = read_rows(name, mime, data)
    with refused(f"import_dossiers {file_id}", "Experio n'a pas pu verifier la liste des dossiers."):
        problems = problems_of(rows)
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:MAX_SHOWN])
        more = f"\n- ... et {len(problems) - MAX_SHOWN} autre(s)" if len(problems) > MAX_SHOWN else ""
        return stop(
            f"`{name}` : aucun dossier cree, le fichier a {len(problems)} probleme(s).\n{shown}{more}\n"
            "Corrigez-le (ou supprimez les lignes en double) et renvoyez-le."
        )
    if not confirmed(session_id, client_confirmed, "je les cree", name):
        listed = "\n".join(row_recap(r) for r in rows[:MAX_SHOWN])
        more = f"\n- ... et {len(rows) - MAX_SHOWN} autres" if len(rows) > MAX_SHOWN else ""
        return question(
            f"`{name}` contient {len(rows)} dossier(s), tous corrects :\n{listed}{more}\nJe les cree ? oui / non"
        )
    if dry_run():
        return ok(f"{TEST_MODE}`{name}` : {len(rows)} dossier(s) seraient crees.")
    created, refused_rows = [], []
    for r in rows:
        try:
            dossier_id = odoo_json("res.company", "create", values_of(r))
        except Exception as error:
            refused_rows.append(f"- Ligne {r['line']} (`{r['name']}`) : {experio_refusal(error)}")
            continue
        attach_user(None, dossier_id)
        created.append(r["name"])
    taken(file_id)
    message = f"`{name}` : {len(created)} dossier(s) cree(s) sur {len(rows)}."
    if refused_rows:
        message += (
            "\nRefuses par Experio :\n" + "\n".join(refused_rows) + "\nCorrigez ces lignes et renvoyez-les seules."
        )
    return ok(message)


# --- A collaborateur or a client ---

KINDS = {  # kind -> (res.users employee_type, exp.user.profile type, the word for the client)
    "collaborator": ("employee", "employee", "collaborateur"),
    "client": ("client", "customer", "client"),
}


def profiles(profile_type):
    """Return the exp.user.profile entries of ``profile_type`` ("employee" for a collaborateur, "customer" for a
    client) as {name: id}: read from Experio, never fixed, since a cabinet adds its own."""
    found = odoo_call(
        "exp.user.profile", "search_read", [("type", "=", profile_type)], fields=["id", "name"], order="name"
    )
    return {p["name"]: p["id"] for p in found}


def person_recap(label, dossier, given, chosen, existing):
    """Return the French read-back the client checks before the person is created, or, when ``existing`` (the
    user found by email) is given, before that user is attached to the dossier."""
    if existing:
        return (
            f"Le {label} `{existing['name']}` existe deja (`{existing['login']}`). Avant de le rattacher, verifiez :\n"
            f"- Dossier : `{dossier}`\n- Profil : `{chosen}`"
        )
    tel = f"- Telephone : {given['phone']}\n" if given.get("phone") else ""
    return (
        f"Avant de creer ce {label}, verifiez ces informations :\n- Nom : `{given['name']}`\n"
        f"- E-mail : `{given['email']}`\n{tel}- Profil : `{chosen}`\n- Dossier : `{dossier}`"
    )


def user_by_email(email, employee_type):
    """Return the user of ``employee_type`` whose login or email is ``email`` ({id, name, login}), or None."""
    found = users_by_email(email, [employee_type], ["id", "name", "login"], 1)
    return found[0] if found else None


@step_tool
def create_person(
    kind: str,
    dossier_id: int,
    name: str = "",
    email: str = "",
    phone: str = "",
    profile: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Create a collaborateur (``kind`` "collaborator") or a client (``kind`` "client") in Experio and give them
    the dossier: a res.users with the email as login and a profile of that kind, added to the dossier's
    Collaborateurs or Clients. A user of that kind with that email already exists: only attached.

    Only values the client wrote themselves are used (``client_message`` and their earlier messages).

    Returns a question result when the name, email or a known ``profile`` is missing, then a question result
    reading the values back until the client said yes (``client_confirmed``, the agent's reading of a clear yes
    to that question); an ok result once created or attached (in test mode, without writing).

    Raises:
        ToolError: unknown ``kind``.
    """
    if kind not in KINDS:
        raise ToolError("kind : collaborator ou client.")
    employee_type, profile_type, label = KINDS[kind]
    dossier = require_dossier(dossier_id)
    words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
    given = {key: value.strip() for key, value in (("name", name), ("email", email), ("phone", phone)) if value}
    not_written = [key for key, value in given.items() if not said(normalized(value), words)]
    given = {key: value for key, value in given.items() if key not in not_written}
    with refused(f"create_person {kind}", "Experio n'a pas pu lire les profils."):
        known = profiles(profile_type)
    chosen = next((p for p in known if normalized(p) == normalized(profile)), None) if profile else None
    missing = []
    if not given.get("name"):
        missing.append("son nom")
    if not given.get("email"):
        missing.append("son e-mail")
    if not chosen:
        missing.append(f"son profil ({', '.join(f'`{p}`' for p in known)})")
    if missing:
        unread = f" ({', '.join(not_written)} : pas ecrit par le client)" if not_written else ""
        return question(f"Pour creer ce {label} sur le dossier `{dossier}`, il me faut {' et '.join(missing)}.{unread}")
    with refused(f"create_person {given['email']}", "Experio n'a pas pu verifier cet utilisateur."):
        existing = user_by_email(given["email"], employee_type)
    recap = person_recap(label, dossier, given, chosen, existing)
    if not confirmed(session_id, client_confirmed, "vous confirmez", given["email"]):
        return question(f"{recap}\nVous confirmez ? oui / non")
    if dry_run():
        return ok(
            f"{TEST_MODE}{label.capitalize()} `{given['name']}` (`{given['email']}`) serait rattache au dossier "
            f"`{dossier}`."
        )
    with refused(f"create_person {given['email']}", f"Experio refuse ce {label} : {{reason}}"):
        if existing:
            user_id = existing["id"]
        else:
            values = {
                "name": given["name"],
                "login": given["email"],
                "email": given["email"],
                "employee_type": employee_type,
                "exp_profile_id": known[chosen],
            }
            if given.get("phone"):
                values["phone"] = given["phone"]
            user_id = odoo_json("res.users", "create", values)
        attach_user(user_id, dossier_id, known[chosen], employee_type)
    made = "rattache" if existing else "cree et rattache"
    return ok(
        f"{label.capitalize()} `{given['name']}` (`{given['email']}`) {made} au dossier `{dossier}`, profil `{chosen}`."
    )


# --- Archiving a dossier ---


@step_tool
def archive_dossier(dossier_id: int, client_confirmed: bool = False, session_id: str = "") -> dict:
    """Archive a dossier, as its form's Archiver does (res.company action_archive_folder: active False): hidden
    from the lists, nothing deleted, restorable from Experio (filtre Archives, Desarchiver).

    Returns a question result with the dossier's invoice counts until the client said yes (``client_confirmed``);
    then an ok result (in test mode, without archiving)."""
    dossier = require_dossier(dossier_id)
    with refused(f"archive_dossier {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu compter ses factures."):
        total = odoo_call("exp.invoice", "search_count", [("company_id", "=", dossier_id)])
        posted = odoo_call(
            "exp.invoice", "search_count", [("company_id", "=", dossier_id), ("state", "in", list(POSTED))]
        )
    if not confirmed(session_id, client_confirmed, "je l'archive", dossier):
        return question(
            f"Je vais archiver le dossier `{dossier}` ({total} facture(s), dont {posted} comptabilisee(s)). "
            "Il disparaitra des listes, rien n'est supprime et il pourra etre restaure. Je l'archive ? oui / non"
        )
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier `{dossier}` serait archive.")
    with refused(f"archive_dossier {dossier_id}", f"Experio refuse d'archiver le dossier `{dossier}` : {{reason}}"):
        odoo_json("res.company", "action_archive_folder", [dossier_id])
    return ok(
        f"Dossier `{dossier}` archive. Il peut etre restaure depuis Experio (filtre Archives, bouton Desarchiver)."
    )
