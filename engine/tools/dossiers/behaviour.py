"""How Tarik behaves for a cabinet: switches and numbers the team changes on the Comportement page
(server/api/behaviour.py), or a manager asks for in a conversation (the Dossier graph's mode behaviour, after
check_manager: manage_behaviour).

They live in each cabinet's own schema (tarik_behaviour): one cabinet's choice never reaches another. A setting never
changed has its default (SETTINGS), read at each use, so a change applies to the next email."""

from psycopg.types.json import Jsonb

from engine.tools.core.app import ToolError, ok, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import refused
from engine.tools.core.queries import Q
from engine.tools.dossiers.people import RANK, sender_of, sender_reach

# Each setting: its kind (flag: on or off; minutes: 0 to MAX_MINUTES, 0 being never), default, name and what it does.
SETTINGS = {
    "acknowledge": {
        "kind": "flag",
        "default": True,
        "name": "Accusé de réception",
        "description": "Répondre aussitôt à un nouvel email que le message est reçu, avant de le traiter.",
    },
    "progress": {
        "kind": "minutes",
        "default": 0,
        "name": "Point d'avancement (minutes)",
        "description": "Pendant un long traitement, écrire au client toutes les N minutes qu'il est toujours en cours "
        "(0 : jamais).",
    },
}
MAX_MINUTES = 120
ON, OFF = {"1", "true", "oui", "on", "activer"}, {"0", "false", "non", "off", "desactiver", "désactiver"}


def checked(key, value):
    """Return ``value`` as setting ``key`` keeps it (a flag a bool, minutes an int); raise ToolError, in plain French,
    when it is not one (the page sends JSON values, the agent text: both are read)."""
    if key not in SETTINGS:
        raise ToolError(f"réglage inconnu : {key} (connus : {', '.join(SETTINGS)})")
    word = str(value).strip().lower()
    if SETTINGS[key]["kind"] == "flag":
        if isinstance(value, bool):
            return value
        if word in ON | OFF:
            return word in ON
        raise ToolError(f"{SETTINGS[key]['name']} : oui ou non")
    if not isinstance(value, bool) and word.isdecimal() and int(word) <= MAX_MINUTES:
        return int(word)
    raise ToolError(f"{SETTINGS[key]['name']} : un nombre de minutes de 0 à {MAX_MINUTES}")


def settings():
    """Return the current cabinet's settings, in SETTINGS' order: [{key, kind, name, description, value, default,
    changed_by, changed_at}], changed_by and changed_at None for a setting never changed."""
    with cabinet_db() as db:
        rows = {key: rest for key, *rest in db.execute(Q.behaviour_listed).fetchall()}
    found = []
    for key, about in SETTINGS.items():
        value, by, at = rows.get(key, (about["default"], None, None))
        found.append({"key": key, **about, "value": value, "changed_by": by, "changed_at": at})
    return found


def behaviour(key):
    """Return the current cabinet's value of setting ``key``: its own, else its default."""
    return next(found["value"] for found in settings() if found["key"] == key)


def change(key, value, author):
    """Set the current cabinet's setting ``key`` to ``value`` (checked first: ToolError) for ``author``; return it as
    kept."""
    kept = checked(key, value)
    with cabinet_db() as db:
        db.execute(Q.behaviour_set, (key, Jsonb(kept), author))
    return kept


def said(found):
    """Return one setting in plain French: « Name : oui / non / N minutes »."""
    value = found["value"]
    if found["kind"] == "flag":
        return f"{found['name']} : {'oui' if value else 'non'}"
    return f"{found['name']} : {value or 'jamais'}"


@step_tool
def manage_behaviour(session_id: str, action: str = "list", key: str = "", value: str = "") -> dict:
    """List the cabinet's behaviour settings (action "list"), or change one (action "update": ``key`` one of SETTINGS,
    ``value`` oui/non for a switch, a number of minutes for a delay). A setting holds for every client of the cabinet:
    in an email, only its Experio admins and Experio's staff change one, as for a routine (manage_routine).

    Returns an ok result saying the settings, or the one changed; a stop result when the key or value is wrong or the
    sender may not."""
    if action == "list":
        return ok("Réglages de comportement :\n" + "\n".join(f"- {said(found)}" for found in settings()))
    if action != "update":
        return stop("Réglages de comportement : action list ou update.")
    with refused("manage_behaviour", "Experio n'a pas pu verifier vos droits."):
        reach = sender_reach(session_id)
    if reach is not None and reach.get("rank", 0) < RANK["admin"]:
        return stop(
            "Ce réglage vaut pour tous les clients du cabinet : seuls ses administrateurs Experio et l'équipe Experio "
            "le changent (ou la page Comportement). Rien n'est changé."
        )
    try:
        change(key, value, sender_of(session_id) or "conversation")
    except ToolError as wrong:
        return stop(f"Rien n'est changé : {wrong}.")
    return ok(f"Réglage changé. {said(next(found for found in settings() if found['key'] == key))}.")
