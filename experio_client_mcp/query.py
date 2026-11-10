"""The general read: what can be read (describe_data) and the reading itself (query_data), over the registry only.

Read only by construction: search_read, search_count and read_group, nothing else exists here. It never takes a model,
field, operator, sort or grouping the registry (registry.py) does not list; the dossier is asked for by id, proven
(experio.require_dossier: one of this user's own), forced into every domain and into Experio's context. Filters are
plain [field, operator, value] triples, all true together: no "or", no path through another model. A port of Tarik's
query (engine/tools/data/query.py): a rule changed there is changed here too."""

import datetime
import re

from experio_client_mcp.experio import Refusal, experio_errors, in_dossier, odoo_call, require_dossier
from experio_client_mcp.registry import (
    CHOICE,
    DATE,
    FLAG,
    GROUPABLE,
    ID,
    LINK,
    MODELS,
    NUMBER,
    OPERATORS,
    SORTABLE,
    TEXT,
    UNSET,
)

DEFAULT_LIMIT, MAX_LIMIT = 50, 200  # rows shown; groups too
MAX_TERMS, MAX_LIST, MAX_TEXT = 12, 50, 80  # filters, values of an "in", characters of a text value
GRANULARITIES = ("day", "week", "month", "quarter", "year")
ORDER = re.compile(r"(?P<name>[a-z_]+(:[a-z]+)?)( (?P<way>asc|desc))?")
DAY = re.compile(r"\d{4}-\d\d-\d\d")


def known(model):
    """Return model ``model``'s registry entry. Raises Refusal, listing the readable models, when there is none."""
    if not isinstance(model, str) or model not in MODELS:
        raise Refusal(f"Modèle « {model} » non lisible. Modèles lisibles : {', '.join(MODELS)}.")
    return MODELS[model]


def column(source, model, name):
    """Return the registry column ``name`` of ``source`` (the entry of ``model``). Raises Refusal for any other."""
    if not isinstance(name, str) or name not in source.columns:
        raise Refusal(f"Champ « {name} » non lisible dans {model}. Champs lisibles : {', '.join(source.columns)}.")
    return source.columns[name]


def describe_data(model=""):
    """Return what query_data can read: the readable models, or one model's columns (name, kind, meaning, allowed
    values,
    filters, whether it can be grouped or summed). Reads nothing of Experio."""
    if not model:
        return {"models": {name: source.label for name, source in MODELS.items()}}
    source = known(model)
    columns = {}
    for name, col in source.columns.items():
        columns[name] = {
            "kind": col.kind,
            "meaning": col.label,
            "filters": list(OPERATORS[col.kind]),
            **({"values": col.values} if col.values else {}),
            **({"groupable": True} if col.kind in GROUPABLE else {}),
            **({"sum": True} if col.kind == NUMBER else {}),
            **({"false_means": "non renseigné"} if col.kind in UNSET else {}),
        }
    return {"model": model, "meaning": source.label, "columns": columns}


def a_day(value):
    """Return whether ``value`` is a plain date, AAAA-MM-JJ, that exists."""
    if not isinstance(value, str) or not DAY.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def a_number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


def an_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# What each kind but a choice takes: its test and how the refusal names it.
EXPECTED = {
    ID: (an_id, "un numéro entier positif"),
    LINK: (an_id, "un numéro entier positif"),
    NUMBER: (a_number, "un nombre"),
    FLAG: (lambda value: isinstance(value, bool), "vrai ou faux (true ou false)"),
    DATE: (a_day, "une date AAAA-MM-JJ"),
    TEXT: (
        lambda value: isinstance(value, str) and len(value.strip()) > 0 and len(value) <= MAX_TEXT,
        f"un texte de 1 à {MAX_TEXT} caractères",
    ),
}


def value_of(col, name, value):
    """Return ``value`` as the type column ``col`` (called ``name``) takes. Raises Refusal when it is not one, or, for a
    choice, not one of its values."""
    if value is False and col.kind in UNSET:  # not set: Experio's false
        return value
    if col.kind == CHOICE:
        if not isinstance(value, str) or value not in col.values:
            raise Refusal(f"{name} : {value!r} inconnu. Valeurs : {', '.join(col.values)}.")
        return value
    valid, expected = EXPECTED[col.kind]
    if not valid(value):
        raise Refusal(f"{name} : {expected} est attendu, pas {value!r}.")
    return value


def term_of(source, model, raw):
    """Return the checked domain term (field, operator, value) of the model's filter ``raw``, a [field, operator, value]
    list. Raises Refusal for any other shape, a field or operator the registry does not allow, or a value of the wrong
    type or outside a choice's values."""
    if not isinstance(raw, list | tuple) or len(raw) != 3:
        raise Refusal(f"Filtre {raw!r} : la forme est [champ, opérateur, valeur].")
    name, operator, value = raw
    col = column(source, model, name)
    if operator not in OPERATORS[col.kind]:
        raise Refusal(f"{name} : opérateur « {operator} » refusé. Permis : {' '.join(OPERATORS[col.kind])}.")
    if operator != "in":
        if value is False and operator not in ("=", "!="):
            raise Refusal(f"{name} : « non renseigné » (false) se demande avec = ou !=.")
        return (name, operator, value_of(col, name, value))
    if not isinstance(value, list) or not 0 < len(value) <= MAX_LIST:
        raise Refusal(f"{name} : « in » prend une liste de 1 à {MAX_LIST} valeurs.")
    return (name, operator, [value_of(col, name, item) for item in value])


def terms_of(source, model, domain):
    """Return the checked terms of ``domain`` (a list of filters, None for none). Raises Refusal as term_of does, or
    when
    there are more than MAX_TERMS."""
    if domain is None:
        return []
    if not isinstance(domain, list) or len(domain) > MAX_TERMS:
        raise Refusal(f"domain : une liste d'au plus {MAX_TERMS} filtres [champ, opérateur, valeur] est attendue.")
    return [term_of(source, model, raw) for raw in domain]


def grouping_of(source, model, spec):
    """Return the group-by spec ``spec`` (a column, a date's as "date:month") checked. Raises Refusal when the column is
    not groupable or the granularity is unknown."""
    spec = str(spec)
    name, _, granularity = spec.partition(":")
    col = column(source, model, name)
    if col.kind not in GROUPABLE:
        groupable = ", ".join(n for n, c in source.columns.items() if c.kind in GROUPABLE)
        raise Refusal(f"{name} : non regroupable. Regroupables : {groupable}.")
    if col.kind == DATE:
        if granularity not in GRANULARITIES:
            raise Refusal(f"{name} : une date se regroupe par période : {', '.join(GRANULARITIES)} (« {name}:month »).")
    elif granularity:
        raise Refusal(f"{name} : seule une date se regroupe par période.")
    return spec


def order_of(order, allowed):
    """Return the sort ``order`` ("champ", "champ desc"; "" for none) checked against ``allowed`` (names, group specs,
    sums). Raises Refusal for anything else."""
    if not order:
        return ""
    found = ORDER.fullmatch(order.strip()) if isinstance(order, str) else None
    if not found or found["name"] not in allowed:
        raise Refusal(f"order « {order} » refusé : {', '.join(allowed)}, puis asc ou desc.")
    return order.strip()


def limit_of(limit):
    """Return ``limit`` as a number of rows (1 to MAX_LIMIT). Raises Refusal for anything but an integer."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise Refusal(f"limit : un entier de 1 à {MAX_LIMIT} est attendu.")
    return min(limit, MAX_LIMIT)


def rounded(row):
    """Return ``row`` with its decimals rounded to cents (Experio's floats carry binary noise)."""
    return {key: round(value, 2) if isinstance(value, float) else value for key, value in row.items()}


def query_data(
    dossier_id, model, domain=None, fields=None, group_by=None, sums=None, order="", limit=DEFAULT_LIMIT, group_limit=2
):
    """Read one of the user's dossiers, read only: records, or counts and sums grouped, over the registry's models and
    columns. Returns {dossier, model, total, shown, rows} (records) or {dossier, model, groups, truncated} (grouped:
    truncated says more groups exist than the limit shows). Raises Refusal for a dossier that is not the user's, a
    model,
    column, operator or value the registry does not allow, or an Experio refusal. ``group_limit``: how many columns a
    grouping may take, for the curated tools: the agent's own query_data always passes two."""
    source = known(model)
    name = require_dossier(dossier_id)
    terms = terms_of(source, model, domain)
    shown = limit_of(limit)
    full = [("company_id", "=", dossier_id), *source.base, *terms]
    context = in_dossier(dossier_id)
    if group_by or sums:
        return grouped(source, model, name, full, context, group_by or [], sums or [], order, shown, group_limit)
    wanted = fields or list(source.columns)
    if not isinstance(wanted, list):
        raise Refusal("fields : une liste de champs est attendue.")
    for each in wanted:
        column(source, model, each)
    ordered = order_of(order, [n for n, c in source.columns.items() if c.kind in SORTABLE])
    with experio_errors("la lecture des données"):
        rows = odoo_call(
            model, "search_read", full, fields=wanted, order=ordered or source.order, limit=shown, context=context
        )
        # a page that is not full is everything there is
        total = len(rows) if len(rows) < shown else odoo_call(model, "search_count", full, context=context)
    return {"dossier": name, "model": model, "total": total, "shown": len(rows), "rows": [rounded(r) for r in rows]}


def grouped(source, model, name, full, context, group_by, sums, order, shown, group_limit=2):
    """Return the answer of a grouped read: one row per group with its count and sums, and whether more groups exist
    than
    ``shown``."""
    if not isinstance(group_by, list) or not isinstance(sums, list):
        raise Refusal("group_by et sums : des listes de champs sont attendues.")
    if not group_by or len(group_by) > group_limit:
        raise Refusal(f"group_by : de un à {group_limit} champs sont attendus (avec sums, au moins un).")
    groups = [grouping_of(source, model, spec) for spec in group_by]
    for each in sums:
        if column(source, model, each).kind != NUMBER:
            numbers = ", ".join(n for n, c in source.columns.items() if c.kind == NUMBER)
            raise Refusal(f"{each} : non sommable. Sommables : {numbers}.")
    asked = order_of(order, [*groups, *sums, "count"])
    asked = "__count" + asked[5:] if asked.split(" ")[0] == "count" else asked
    totals = [f"{each}:sum" for each in sums]
    with experio_errors("la lecture des données"):
        found = odoo_call(
            model, "read_group", full, totals, groups, lazy=False, orderby=asked, limit=shown + 1, context=context
        )
    keep = {"__count", *groups, *sums, *(spec.split(":")[0] for spec in groups)}
    rows = [rounded({("count" if k == "__count" else k): v for k, v in row.items() if k in keep}) for row in found]
    return {"dossier": name, "model": model, "groups": rows[:shown], "truncated": len(rows) > shown}
