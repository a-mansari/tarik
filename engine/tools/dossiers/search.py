"""Finding the client's dossier."""

from engine.tools.core import words
from engine.tools.core.app import ToolError, log, ok, routine, step_tool, stop, tool
from engine.tools.core.config import DOSSIER_DOMAIN, MIN_SIMILARITY
from engine.tools.core.experio import odoo_call

UNAVAILABLE = "Experio n'a pas pu donner la liste des dossiers."


def dossiers():
    """Return every dossier (client company) Tarik may work in (DOSSIER_DOMAIN), as [{id, name}] by name."""
    return odoo_call("res.company", "search_read", DOSSIER_DOMAIN, fields=["id", "name"], order="name")


def dossiers_or_none(step):
    """Return dossiers(), or None when Experio can't give them (the error is logged under ``step``)."""
    try:
        return dossiers()
    except Exception as error:
        log(f"{step}: {error}")
        return None


# Experio's own states of a dossier (res.company state), as the client reads them.
STATES = {"active": "actifs", "new": "nouveaux", "archived": "archives"}


@tool
def list_dossiers(names: bool = False) -> str:
    """Return how many dossiers the cabinet has in Experio, by res.company state (actifs, nouveaux, archives),
    as French text; UNAVAILABLE when Experio can't answer. ``names``: also list the names of the dossiers not
    archived; the agent sets it only when the client asks for the names themselves."""
    try:
        groups = odoo_call(
            "res.company", "read_group", [*DOSSIER_DOMAIN, ("active", "in", [True, False])], ["state"], ["state"]
        )
    except Exception as error:
        log(f"list_dossiers: {error}")
        return UNAVAILABLE
    counts = {group["state"]: group["state_count"] for group in groups}
    by_state = ", ".join(f"{counts.get(state, 0)} {label}" for state, label in STATES.items())
    text = f"{sum(counts.values())} dossiers : {by_state}."
    if not names:
        return text
    found = dossiers_or_none("list_dossiers")
    if found is None:
        return UNAVAILABLE
    return f"{text}\nDossiers (hors archives) :\n" + "\n".join(f"- {c['name']}" for c in found)


@tool
def find_dossier(name: str) -> str:
    """Find the dossier the client means from the ``name`` they gave, even misspelled or partial.

    Returns French text: up to 5 closest dossiers with their id and similarity, best first; a line saying none
    is close; or UNAVAILABLE. One clear match: use its id. Several close ones: ask the client."""
    found = dossiers_or_none("find_dossier")
    if found is None:
        return UNAVAILABLE
    close = words.ranked(name, found, MIN_SIMILARITY, top=5)
    if not close:
        return f"Aucun dossier ne ressemble a « {name} »."
    return "\n".join(f"- {c['name']} (id {c['id']}) : ressemblance {round(score * 100)} %" for score, c in close)


@step_tool
def resolve_dossier(name: str) -> dict:
    """Pick the dossier a run works in from the ``name`` the client gave (even misspelled or partial).

    Returns:
        - a routine result with ``dossier_id`` when exactly one dossier matches at 100 %;
        - an ok result with ``dossier_id`` (and tell=True, so the choice is reported to the client) when only
          one dossier is close;
        - otherwise a stop result for the client: no name given, none close, or the closest ones to choose
          among (the client's next message gives the name).

    Raises:
        ToolError: Experio can't give the list of dossiers.
    """
    found = dossiers_or_none("resolve_dossier")
    if found is None:
        raise ToolError(UNAVAILABLE)
    name = (name or "").strip()
    if not name:
        return stop("Pour quel dossier ? Rien n'a été traité.")
    close = words.ranked(name, found, MIN_SIMILARITY, top=5)
    if not close:
        return stop(f"Aucun dossier ne ressemble a « {name} » : quel est le nom du dossier ? Rien n'a été traité.")
    exact = [dossier for score, dossier in close if round(score * 100) == 100]
    if len(exact) == 1:
        return routine(f"Dossier `{exact[0]['name']}`.", dossier_id=exact[0]["id"])
    if len(close) == 1:  # the only dossier near the name: taken, and the client told which
        dossier = close[0][1]
        return ok(
            f"Dossier `{dossier['name']}` retenu : le seul proche de « {name} ».", dossier_id=dossier["id"], tell=True
        )
    names = " ; ".join(f"`{dossier['name']}`" for _, dossier in close)
    return stop(f"Quel dossier est « {name} » ? Les plus proches : {names}. Rien n'a été traité.")
