"""Manage dossiers and who works on them, as Experio's Folders and Users screens do: create the dossiers listed in a
spreadsheet, create and invite a collaborateur or a client and give them a dossier, give or take a dossier, change a
role, send an invitation again, archive a dossier.

Each tool reads the values back and writes only once the client said yes to that read-back (identity.confirmed):
a res.company or a res.users cannot be deleted from here, and only the client can catch a misread digit.
"""

import csv
import io

from engine.tools.core.app import ToolError, ok, question, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import dry_run, experio_refusal, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.words import ICE_WRITTEN, counted, normalized, said
from engine.tools.dossiers.identity import FOLDER_TYPES, PLACEHOLDER_IDS, confirmed, having, real_ids
from engine.tools.dossiers.people import RANK, attach_user, level_of, sender_reach, users_by_email
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
            f"`{name}` : il manque des colonnes dans la première ligne : {', '.join(missing)}. "
            "Les colonnes attendues sont : Nom, Type, ICE, IF, Mode de déclaration (puis Pays, Ville, Code postal, "
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
        raise ToolError(f"`{name}` : aucune ligne de dossier sous l'en-tête.")
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
            f"{where} : mode de déclaration « {row['declaration_mode_given']} » inconnu "
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
            problems.append(f"{where} : le dossier `{existing[key]}` existe déjà")
        if row["ice"] and row["ice"] not in PLACEHOLDER_IDS:
            if row["ice"] in seen_ices:
                problems.append(f"{where} : même ICE que la ligne {seen_ices[row['ice']]}")
            seen_ices.setdefault(row["ice"], row["line"])
        ids = real_ids((("ice", row["ice"]), ("vat", row["vat"])))
        same = having(ids) if ids else []
        if same:
            problems.append(f"{where} : l'ICE ou l'IF est déjà celui du dossier `{same[0]['name']}`")
    return problems


def row_recap(row):
    """Return one dossier of the file as a French line the client reads back."""
    parts = [
        FOLDER_TYPES[row["folder_type"]],
        f"ICE {row['ice'] or 'non renseigné'}",
        f"IF {row['vat'] or 'non renseigné'}",
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
    with refused(f"import_dossiers {file_id}", "Experio n'a pas pu vérifier la liste des dossiers."):
        problems = problems_of(rows)
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:MAX_SHOWN])
        others = counted(len(problems) - MAX_SHOWN, "{} autre", "{} autres")
        more = f"\n- ... et {others}" if len(problems) > MAX_SHOWN else ""
        return stop(
            f"`{name}` : je n'ai créé aucun dossier, car le fichier a "
            f"{counted(len(problems), '{} problème', '{} problèmes')} :\n{shown}{more}\n\n"
            "Corrigez-le (ou supprimez les lignes en double) et renvoyez-le."
        )
    ask = counted(len(rows), "Je le crée", "Je les crée")
    if not confirmed(session_id, client_confirmed, ask, name):
        listed = "\n".join(row_recap(r) for r in rows[:MAX_SHOWN])
        others = counted(len(rows) - MAX_SHOWN, "{} autre", "{} autres")
        more = f"\n- ... et {others}" if len(rows) > MAX_SHOWN else ""
        return question(
            f"`{name}` contient {counted(len(rows), '{} dossier, correct', '{} dossiers, tous corrects')} :\n"
            f"{listed}{more}\n\n{ask} ? oui / non"
        )
    if dry_run():
        would = counted(len(rows), "{} dossier serait créé", "{} dossiers seraient créés")
        return ok(f"{TEST_MODE}`{name}` : {would}.")
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
    message = f"`{name}` : {counted(len(created), '{} dossier créé', '{} dossiers créés')} sur {len(rows)}."
    if refused_rows:
        message += (
            "\nExperio a refusé ces lignes :\n"
            + "\n".join(refused_rows)
            + "\n\nCorrigez ces lignes et renvoyez-les seules."
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
            f"Le {label} `{existing['name']}` existe déjà (`{existing['login']}`). Avant de le rattacher, vérifiez :\n"
            f"- Dossier : `{dossier}`\n- Profil : `{chosen}`"
        )
    tel = f"- Téléphone : {given['phone']}\n" if given.get("phone") else ""
    return (
        f"Avant de créer ce {label}, vérifiez ces informations :\n- Nom : `{given['name']}`\n"
        f"- E-mail : `{given['email']}`\n{tel}- Profil : `{chosen}`\n- Dossier : `{dossier}`"
    )


def user_by_email(email, employee_type):
    """Return the user of ``employee_type`` whose login or email is ``email`` ({id, name, login, employee_state,
    exp_profile_id}), or None."""
    found = users_by_email(email, [employee_type], ["id", "name", "login", "employee_state", "exp_profile_id"], 1)
    return found[0] if found else None


def invited(user_id, email):
    """Send Experio's invitation to a user never invited (employee_state « new »), as its « Inviter » button does
    (res.users action_invite_user: a password made, the global Experio user, the invitation email with login and
    password, the user active). Returns the sentence for the reply: sent, or Experio's refusal (seats all taken...)."""
    try:
        odoo_json("res.users", "action_invite_user", [user_id])
    except Exception as error:  # the user stays made and on the dossier: only the invitation is to send again
        why = experio_refusal(error)
        return (
            f" Je n'ai pas pu envoyer l'invitation, car Experio refuse : {why}. "
            "Le cabinet peut la renvoyer depuis Experio (bouton Inviter)."
        )
    return f" J'ai envoyé l'invitation Experio à `{email}`, avec son identifiant et son mot de passe."


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
        unread = f" ({', '.join(not_written)} : pas écrit par le client)" if not_written else ""
        return question(f"Pour créer ce {label} sur le dossier `{dossier}`, il me faut {' et '.join(missing)}.{unread}")
    with refused(f"create_person {given['email']}", "Experio n'a pas pu vérifier cet utilisateur."):
        existing = user_by_email(given["email"], employee_type)
    with refused(f"create_person {given['email']}", "Experio n'a pas pu vérifier vos droits."):
        reach = sender_reach(session_id)
        why = beyond_reach(reach, dossier_id, dossier, known[chosen])
        if not why and existing and reach and RANK[level_of(existing["exp_profile_id"])] > reach["rank"]:
            why = f"`{existing['name']}` a plus de droits que `{reach['name']}`"
    if why:
        return stop(f"Pas d'accès créé pour `{given['email']}` : {why}. Rien n'est changé.")
    recap = person_recap(label, dossier, given, chosen, existing)
    if not confirmed(session_id, client_confirmed, "vous confirmez", given["email"]):
        return question(f"{recap}\n\nVous confirmez ? oui / non")
    if dry_run():
        return ok(
            f"{TEST_MODE}{label.capitalize()} `{given['name']}` (`{given['email']}`) serait rattaché au dossier "
            f"`{dossier}`."
        )
    with refused(f"create_person {given['email']}", f"Experio refuse ce {label} : {{reason}}"):
        if existing:
            user_id = existing["id"]
        else:
            # As the Users form makes one: the e-mail is the login only, and no Odoo sign-up mail (auth_signup's, sent
            # to a user made with an e-mail): Experio's own invitation follows (invited).
            values = {
                "name": given["name"],
                "login": given["email"],
                "employee_type": employee_type,
                "exp_profile_id": known[chosen],
            }
            if given.get("phone"):
                values["phone"] = given["phone"]
            user_id = odoo_json("res.users", "create", values, context={"no_reset_password": True})
        # A dossier's profile row is written by Experio's admins only (ir.model.access.csv line 23): for another
        # sender, the person has their profile everywhere (exp_profile_id) and none of their own on the dossier.
        on_dossier = known[chosen] if reach is None or reach["rank"] >= RANK["admin"] else None
        attach_user(user_id, dossier_id, on_dossier, employee_type)
    made = "rattaché" if existing else "créé et rattaché"
    sent = invited(user_id, given["email"]) if not existing or existing["employee_state"] == "new" else ""
    return ok(
        f"{label.capitalize()} `{given['name']}` (`{given['email']}`) {made} au dossier `{dossier}`, "
        f"profil `{chosen}`.{sent}"
    )


def beyond_reach(reach, dossier_id, dossier, profile_id=False, user=None):
    """Return why the sender (sender_reach's ``reach``; None: no limit) may not make this change, else "": a dossier
    not theirs, a profile above their own, someone above them, their own role (``user`` with ``profile_id``), or a
    dossier's profile, which only Experio's admins write (exp.user.company.profile, ir.model.access.csv line 23)."""
    if reach is None:
        return ""
    if dossier_id and dossier_id not in reach["company_ids"]:
        return f"le dossier `{dossier}` n'est pas attribué à `{reach['name']}` dans Experio"
    if profile_id and RANK[level_of(profile_id)] > reach["rank"]:
        return f"ce profil donne plus de droits que celui de `{reach['name']}`"
    if user and RANK[level_of(user["exp_profile_id"])] > reach["rank"]:
        return f"`{user['name']}` a plus de droits que `{reach['name']}`"
    if user and profile_id and user["id"] == reach["id"]:
        return "on ne change pas son propre profil"
    if user and profile_id and dossier_id and reach["rank"] < RANK["admin"]:
        return "seuls les administrateurs d'Experio changent le profil d'une personne sur un dossier"
    return ""


# --- A person's access: a dossier given or taken, a role, an invitation sent again ---

ACCESS = {  # action -> what the read-back says is done
    "assign": "lui donner le dossier",
    "unassign": "lui retirer le dossier",
    "role": "changer son profil",
    "invite": "lui renvoyer l'invitation Experio (un nouveau mot de passe, l'ancien ne marchera plus)",
}
PERSON_FIELDS = ["name", "login", "employee_type", "employee_state", "company_ids", "exp_profile_id"]


def person_named(email):
    """Return the collaborateur or client whose login or email is ``email`` (PERSON_FIELDS), None for none or two."""
    found = users_by_email(email, ["employee", "client"], PERSON_FIELDS, 2)
    return found[0] if len(found) == 1 else None


def dossier_profile(user_id, dossier_id, profile_id):
    """Set the user's profile on the dossier (« Profils par Dossier », exp.user.company.profile: one row per user and
    dossier, exp_user_company_profile.py _sql_constraints), as its editable list does."""
    row = odoo_call("exp.user.company.profile", "search", [("user_id", "=", user_id), ("company_id", "=", dossier_id)])
    if row:
        odoo_call("exp.user.company.profile", "write", row, {"profile_id": profile_id})
    else:
        values = {"user_id": user_id, "company_id": dossier_id, "profile_id": profile_id}
        odoo_call("exp.user.company.profile", "create", values)


def access_change(action, user, dossier_id, profile_id):
    """Make the change, as Experio's own screens do; return what was done (French).

    - assign / unassign: the dossier's Collaborateurs or Clients (employee_ids / contact_ids, Experio keeps the
      user's company_ids in step, base_client res_company.py write); assign also sets the dossier's profile when one
      is named (dossier_profile).
    - role: with a dossier, the user's profile on that dossier (« Profils par Dossier », exp.user.company.profile);
      else their profile (exp_profile_id; Experio recomputes their rights, res_users.py write).
    - invite: « Inviter » (action_invite_or_reinvite_user): the first invitation, or a new password sent again.
    """
    members = "employee_ids" if user["employee_type"] == "employee" else "contact_ids"
    if action == "assign":
        attach_user(user["id"], dossier_id, None, user["employee_type"])
        if profile_id:
            dossier_profile(user["id"], dossier_id, profile_id)
        return "dossier donné"
    if action == "unassign":
        odoo_call("res.company", "write", [dossier_id], {members: [(3, user["id"])]})
        return "dossier retiré"
    if action == "invite":
        odoo_json("res.users", "action_invite_or_reinvite_user", [user["id"]])
        return "invitation Experio envoyée"
    if dossier_id:
        dossier_profile(user["id"], dossier_id, profile_id)
        return "profil changé sur ce dossier"
    odoo_call("res.users", "write", [user["id"]], {"exp_profile_id": profile_id})
    return "profil changé"


@step_tool
def change_access(
    action: str,
    email: str,
    dossier_id: int = 0,
    profile: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Change an existing collaborateur's or client's access in Experio: ``action`` "assign" (give them dossier
    ``dossier_id``, with ``profile`` there when named), "unassign" (take it back), "role" (their ``profile``: on
    ``dossier_id`` only when given, else everywhere) or "invite" (Experio's invitation, sent again with a new
    password). The person is found by the ``email`` the client wrote themselves.

    Returns a question result when the email is not the client's own words, no single person has it, the dossier or
    a known profile is missing; then a question result reading the change back until the client said yes
    (``client_confirmed``); a routine result when nothing is to change; an ok result once done (in test mode,
    without writing).

    Raises:
        ToolError: unknown ``action``; Experio's refusal.
    """
    if action not in ACCESS:
        raise ToolError("action : assign, unassign, role ou invite.")
    email = (email or "").strip()
    words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
    if not email or not said(normalized(email), words):
        return question("De qui s'agit-il ? Donnez son e-mail (son identifiant Experio).")
    with refused(f"change_access {email}", "Experio n'a pas pu lire cet utilisateur."):
        user = person_named(email)
    if not user:
        return question(f"Personne n'a l'e-mail `{email}` parmi les collaborateurs et clients d'Experio. Lequel ?")
    dossier = require_dossier(dossier_id, opened=False) if dossier_id else ""  # Tarik may give access to it
    if action in ("assign", "unassign") and not dossier:
        return question(f"Quel dossier pour `{user['name']}` ?")
    label = "collaborateur" if user["employee_type"] == "employee" else "client"
    profile_id, chosen = False, ""
    if profile or action == "role":
        with refused(f"change_access {email}", "Experio n'a pas pu lire les profils."):
            known = profiles(KINDS["collaborator" if label == "collaborateur" else "client"][1])
        chosen = next((p for p in known if normalized(p) == normalized(profile)), None) if profile else None
        if not chosen:
            return question(f"Quel profil pour `{user['name']}` ? {', '.join(f'`{p}`' for p in known)}")
        profile_id = known[chosen]
    with refused(f"change_access {email}", "Experio n'a pas pu vérifier vos droits."):
        why = beyond_reach(sender_reach(session_id), dossier_id, dossier, profile_id, user)
    if why:
        return stop(f"Pas de changement d'accès pour `{user['name']}` : {why}. Rien n'est changé.")
    on = dossier_id in user["company_ids"]
    if action == "role" and dossier and not on:
        return question(
            f"`{user['name']}` n'a pas le dossier `{dossier}` : je le lui donne avec le profil `{chosen}` ?"
        )
    if (action == "assign" and on and not chosen) or (action == "unassign" and not on):
        state = "a déjà" if on else "n'a pas"
        return routine(f"Le {label} `{user['name']}` {state} le dossier `{dossier}` : rien à changer.")
    recap = f"Pour le {label} `{user['name']}` (`{user['login']}`) : {ACCESS[action]}"
    recap += f"\n- Dossier : `{dossier}`" if dossier else ""
    recap += f"\n- Profil : `{chosen}`" if chosen else ""
    if not confirmed(session_id, client_confirmed, "vous confirmez", user["login"], ACCESS[action], dossier, chosen):
        return question(f"{recap}\n\nVous confirmez ? oui / non")
    if dry_run():
        return ok(f"{TEST_MODE}{recap.splitlines()[0]} : serait fait.")
    with refused(f"change_access {email}", "Experio refuse ce changement : {reason}"):
        done = access_change(action, user, dossier_id, profile_id)
    where = f" (dossier `{dossier}`)" if dossier else ""
    shown = f", profil `{chosen}`" if chosen else ""
    return ok(f"{label.capitalize()} `{user['name']}` (`{user['login']}`) : {done}{where}{shown}.")


# --- Archiving a dossier ---


@step_tool
def archive_dossier(dossier_id: int, client_confirmed: bool = False, session_id: str = "") -> dict:
    """Archive a dossier, as its form's Archiver does (res.company action_archive_folder: active False): hidden
    from the lists, nothing deleted, restorable from Experio (filtre Archivé, Désarchiver).

    Returns a question result with the dossier's invoice counts until the client said yes (``client_confirmed``);
    then an ok result (in test mode, without archiving)."""
    dossier = require_dossier(dossier_id, opened=False)
    with refused(f"archive_dossier {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu compter ses factures."):
        total = odoo_call("exp.invoice", "search_count", [("company_id", "=", dossier_id)])
        posted = odoo_call(
            "exp.invoice", "search_count", [("company_id", "=", dossier_id), ("state", "in", list(POSTED))]
        )
    if not confirmed(session_id, client_confirmed, "je l'archive", dossier):
        return question(
            f"Je vais archiver le dossier `{dossier}`, qui a {counted(total, '{} facture', '{} factures')}, "
            f"dont {counted(posted, '{} comptabilisée', '{} comptabilisées')}. "
            "Il disparaîtra des listes, mais rien n'est supprimé et il pourra être restauré. Je l'archive ? oui / non"
        )
    if dry_run():
        return ok(f"{TEST_MODE}Le dossier `{dossier}` serait archivé.")
    with refused(f"archive_dossier {dossier_id}", f"Experio refuse d'archiver le dossier `{dossier}` : {{reason}}"):
        odoo_json("res.company", "action_archive_folder", [dossier_id])
    return ok(
        f"Dossier `{dossier}` archivé. Il peut être restauré depuis Experio (filtre Archivé, bouton Désarchiver)."
    )
