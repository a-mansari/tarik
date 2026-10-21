"""Finding the client's dossier."""

from tarik.core import words
from tarik.core.app import ToolError, log, ok, routine, step_tool, stop, tool
from tarik.core.config import DOSSIER_DOMAIN, MIN_SIMILARITY
from tarik.core.experio import odoo_call

UNAVAILABLE = "Experio n'a pas pu donner la liste des dossiers."


def dossiers():
    """Every dossier (client company) Tarik may work in, as [{id, name}]."""
    return odoo_call("res.company", "search_read", DOSSIER_DOMAIN, fields=["id", "name"], order="name")


def dossiers_or_none(step):
    """dossiers(), or None when Experio can't give them (logged under ``step``)."""
    try:
        return dossiers()
    except Exception as error:
        log(f"{step}: {error}")
        return None


# Experio's own states of a dossier (res.company state), as the client reads them.
STATES = {"active": "actifs", "new": "nouveaux", "archived": "archives"}


@tool
def list_dossiers(names: bool = False) -> str:
    """How many dossiers (client companies) the cabinet has in Experio, by state: actifs, nouveaux, archives.
    ``names``: also the names of those not archived, only when the client asks for the list of names itself."""
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
    """Find the dossier the client means from the name they gave, even
    misspelled or partial. Returns the closest dossiers with their id, best
    first. One clear match: use its id. Several close ones: ask the client."""
    found = dossiers_or_none("find_dossier")
    if found is None:
        return UNAVAILABLE
    close = words.ranked(name, found, MIN_SIMILARITY, top=5)
    if not close:
        return f"Aucun dossier ne ressemble a « {name} »."
    return "\n".join(f"- {c['name']} (id {c['id']}) : ressemblance {round(score * 100)} %" for score, c in close)


@step_tool
def resolve_dossier(name: str) -> dict:
    """The dossier a run works in, from the name the client gave (even misspelled or partial): ok with its
    dossier_id when exactly one dossier matches at 100 %. Otherwise a stop whose message the client reads: no
    name, none close, or the closest ones to choose among (the client's next message gives the name)."""
    found = dossiers_or_none("resolve_dossier")
    if found is None:
        raise ToolError(UNAVAILABLE)
    name = (name or "").strip()
    if not name:
        return stop("Pour quel dossier sont ces fichiers ? Rien n'a ete traite.")
    close = words.ranked(name, found, MIN_SIMILARITY, top=5)
    if not close:
        return stop(f"Aucun dossier ne ressemble a « {name} » : quel est le nom du dossier ? Rien n'a ete traite.")
    exact = [dossier for score, dossier in close if round(score * 100) == 100]
    if len(exact) == 1:
        return routine(f"Dossier {exact[0]['name']}.", dossier_id=exact[0]["id"])
    if len(close) == 1:  # the only dossier near the name: taken, and the client told which
        dossier = close[0][1]
        return ok(f"Dossier {dossier['name']} retenu : le seul proche de « {name} ».", dossier_id=dossier["id"])
    names = " ; ".join(dossier["name"] for _, dossier in close)
    return stop(f"Quel dossier est « {name} » ? Les plus proches : {names}. Rien n'a ete traite.")
