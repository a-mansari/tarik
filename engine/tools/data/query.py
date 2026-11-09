"""The agent's two general read tools: what can be read (describe_data) and the reading itself (query_data).

query_data is read only by construction: it can call search_read, search_count and read_group, nothing else exists in
this module. It never takes a model, field, operator, sort or grouping the registry (registry.py) does not list; the
dossier is asked for by id, proven (require_dossier), forced into every domain and into Experio's context, and the
sender must be allowed to work on it (check_sender). Filters are plain [field, operator, value] triples, all of them
true together: no "or", no path through another model."""

import datetime
import json
import re

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.experio import in_dossier, odoo_call, refused, require_dossier
from engine.tools.data.registry import (
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
)
from engine.tools.dossiers.people import check_sender

DEFAULT_LIMIT, MAX_LIMIT = 50, 200  # rows shown; groups too
MAX_TERMS, MAX_LIST, MAX_TEXT = 12, 50, 80  # filters, values of an "in", characters of a text value
MAX_CHARS = 40000  # an answer longer than this is refused, never cut: a cut answer reads as the whole
GRANULARITIES = ("day", "week", "month", "quarter", "year")
ORDER = re.compile(r"(?P<name>[a-z_]+(:[a-z]+)?)( (?P<way>asc|desc))?")


def known(model):
    """Return model ``model``'s registry entry. Raises ToolError, listing the readable models, when there is none."""
    if model not in MODELS:
        raise ToolError(f"Modèle « {model} » non lisible. Modèles lisibles : {', '.join(MODELS)}.")
    return MODELS[model]


def column(source, model, name):
    """Return the registry column ``name`` of ``source`` (the entry of ``model``). Raises ToolError for any other."""
    if name not in source.columns:
        raise ToolError(f"Champ « {name} » non lisible dans {model}. Champs lisibles : {', '.join(source.columns)}.")
    return source.columns[name]


def text_of(model, source):
    """Return the French description of one model: each column's name, kind, meaning, values and filters allowed."""
    lines = [f"{model} : {source.label}. Les lectures se font toujours dans un seul dossier."]
    for name, col in source.columns.items():
        values = " ; ".join(f"{key} = {meaning}" for key, meaning in col.values.items())
        more = f" Valeurs : {values}." if values else ""
        use = []
        if col.kind in GROUPABLE:
            use.append("regroupable")
        if col.kind == NUMBER:
            use.append("sommable")
        extra = f" ({', '.join(use)})" if use else ""
        lines.append(f"- {name} [{col.kind}] : {col.label}.{more} Filtres : {' '.join(OPERATORS[col.kind])}{extra}")
    return "\n".join(lines)


@tool
def describe_data(model: str = "") -> str:
    """Return, as French text, what query_data can read: without ``model`` the readable models, with one its columns
    (name, kind, meaning, allowed values, the filters each takes, whether it can be grouped or summed).

    Args:
        model: a model named by the list (exp.invoice, exp.move.line...); empty for the list.

    Raises:
        ToolError: ``model`` is not a readable model."""
    if not model:
        listed = [f"- {name} : {source.label}" for name, source in MODELS.items()]
        return "Modèles lisibles avec query_data :\n" + "\n".join(listed)
    return text_of(model, known(model))


def number(value, kind, name):
    """Return ``value`` as the type a ``kind`` column takes. Raises ToolError when it is not one."""
    if kind in (ID, LINK):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ToolError(f"{name} : un numéro entier positif est attendu, pas {value!r}.")
    elif kind == NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolError(f"{name} : un nombre est attendu, pas {value!r}.")
    elif kind == FLAG:
        if not isinstance(value, bool):
            raise ToolError(f"{name} : vrai ou faux (true ou false) est attendu, pas {value!r}.")
    elif kind == DATE:
        try:
            datetime.date.fromisoformat(value)
        except TypeError, ValueError:
            raise ToolError(f"{name} : une date AAAA-MM-JJ est attendue, pas {value!r}.") from None
    elif kind == TEXT and (not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT):
        raise ToolError(f"{name} : un texte de 1 à {MAX_TEXT} caractères est attendu.")
    return value


def term_of(source, model, raw):
    """Return the checked domain term (field, operator, value) of the model's filter ``raw``, a [field, operator,
    value] list. Raises ToolError for any other shape, a field or operator the registry does not allow, or a value
    of the wrong type or outside a choice's values."""
    if not isinstance(raw, list | tuple) or len(raw) != 3:
        raise ToolError(f"Filtre {raw!r} : la forme est [champ, opérateur, valeur].")
    name, operator, value = raw
    col = column(source, model, name)
    if operator not in OPERATORS[col.kind]:
        raise ToolError(f"{name} : opérateur « {operator} » refusé. Permis : {' '.join(OPERATORS[col.kind])}.")
    if operator == "in":
        if not isinstance(value, list) or not 0 < len(value) <= MAX_LIST:
            raise ToolError(f"{name} : « in » prend une liste de 1 à {MAX_LIST} valeurs.")
        values = [number(item, col.kind, name) for item in value]
    else:
        values = number(value, col.kind, name)
    for item in values if operator == "in" else [values]:
        if col.kind == CHOICE and item not in col.values:
            raise ToolError(f"{name} : « {item} » inconnu. Valeurs : {', '.join(col.values)}.")
    return (name, operator, values)


def terms_of(source, model, domain):
    """Return the checked terms of ``domain`` (a list of filters, None for none). Raises ToolError as term_of does,
    or when there are more than MAX_TERMS."""
    if domain is None:
        return []
    if not isinstance(domain, list) or len(domain) > MAX_TERMS:
        raise ToolError(f"domain : une liste d'au plus {MAX_TERMS} filtres [champ, opérateur, valeur] est attendue.")
    return [term_of(source, model, raw) for raw in domain]


def grouping_of(source, model, spec):
    """Return the group-by spec ``spec`` (a column, a date's as "date:month") checked. Raises ToolError when the
    column is not groupable or the granularity is unknown."""
    name, _, granularity = spec.partition(":")
    col = column(source, model, name)
    if col.kind not in GROUPABLE:
        raise ToolError(f"{name} : non regroupable. Regroupables : {groupable(source)}.")
    if col.kind == DATE:
        if granularity not in GRANULARITIES:
            raise ToolError(
                f"{name} : une date se regroupe par période : {', '.join(GRANULARITIES)} (« {name}:month »)."
            )
    elif granularity:
        raise ToolError(f"{name} : seule une date se regroupe par période.")
    return spec


def groupable(source):
    """Return the names of ``source``'s groupable columns, comma separated."""
    return ", ".join(name for name, col in source.columns.items() if col.kind in GROUPABLE)


def order_of(source, model, order, allowed):
    """Return the sort ``order`` ("champ", "champ desc"; "" for none) checked against ``allowed`` (names, group
    specs, sums). Raises ToolError for anything else."""
    if not order:
        return ""
    found = ORDER.fullmatch(order.strip())
    if not found or found["name"] not in allowed:
        raise ToolError(f"order « {order} » refusé : {', '.join(allowed)}, puis asc ou desc.")
    return order.strip()


def limit_of(limit):
    """Return ``limit`` as a number of rows (1 to MAX_LIMIT). Raises ToolError for anything but an integer."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ToolError(f"limit : un entier de 1 à {MAX_LIMIT} est attendu.")
    return min(limit, MAX_LIMIT)


def rounded(row):
    """Return ``row`` with its decimals rounded to cents (Experio's floats carry binary noise)."""
    return {key: round(value, 2) if isinstance(value, float) else value for key, value in row.items()}


def answered(payload):
    """Return ``payload`` as JSON text. Raises ToolError when it is longer than MAX_CHARS."""
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > MAX_CHARS:
        raise ToolError("Résultat trop long : réduisez limit, demandez moins de champs ou ajoutez des filtres.")
    return text


@tool
def query_data(
    dossier_id: int,
    model: str,
    session_id: str,
    domain: list | None = None,
    fields: list | None = None,
    group_by: list | None = None,
    sums: list | None = None,
    order: str = "",
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Read one dossier's data in Experio, read only: records, or counts and sums grouped. Models and columns come
    from describe_data; nothing else can be read.

    Args:
        dossier_id: the dossier (from find_dossier). Every read stays inside it.
        model: a readable model (describe_data).
        session_id: the conversation (filled in by Tarik).
        domain: filters, all true together, each [column, operator, value], e.g. [["state", "=", "post"],
            ["invoice_date", ">=", "2026-01-01"]]. Operators depend on the column's kind (describe_data); "in" takes
            a list; a date is AAAA-MM-JJ; a choice takes one of its values.
        fields: the columns to return (all when empty); records mode only.
        group_by: up to two columns to group by, a date by period ("invoice_date:month"); with it (or sums) the answer
            is one row per group with its count and sums.
        sums: number columns to total per group (grouped mode), e.g. ["amount_untaxed"].
        order: a returned column (or group or sum), then asc or desc.
        limit: rows (or groups) shown, 1 to 200; the answer says how many exist.

    Returns JSON: records mode {dossier, model, total, shown, rows}; grouped mode {dossier, model, groups}.

    Raises:
        ToolError: the dossier is unknown or not the sender's, the model, a column, an operator or a value is not
            allowed, or Experio can't answer."""
    source = known(model)
    name = require_dossier(dossier_id)
    allowed = check_sender(session_id, dossier_id)
    if allowed["status"] == "stop":
        raise ToolError(allowed["message"])
    terms = terms_of(source, model, domain)
    shown = limit_of(limit)
    full = [("company_id", "=", dossier_id), *source.base, *terms]
    context = in_dossier(dossier_id)
    log(f"query_data dossier {dossier_id} {model} {terms} group {group_by} sums {sums}")
    if group_by or sums:
        return grouped(source, model, name, full, context, group_by or [], sums or [], order, shown)
    wanted = fields or list(source.columns)
    if not isinstance(wanted, list):
        raise ToolError("fields : une liste de champs est attendue.")
    for each in wanted:
        column(source, model, each)
    ordered = order_of(source, model, order, [n for n, c in source.columns.items() if c.kind in SORTABLE])
    with refused(f"query_data {model}", f"Experio n'a pas pu lire {model} : {{reason}}"):
        rows = odoo_call(
            model, "search_read", full, fields=wanted, order=ordered or source.order, limit=shown, context=context
        )
        total = odoo_call(model, "search_count", full, context=context)
    return answered({"dossier": name, "model": model, "total": total, "shown": len(rows),
                     "rows": [rounded(row) for row in rows]})  # fmt: skip


def grouped(source, model, name, full, context, group_by, sums, order, shown):
    """Return the answer of a grouped read: one row per group with its count and sums (JSON text)."""
    if not isinstance(group_by, list) or not isinstance(sums, list):
        raise ToolError("group_by et sums : des listes de champs sont attendues.")
    if not group_by or len(group_by) > 2:
        raise ToolError("group_by : un ou deux champs sont attendus (avec sums, au moins un).")
    groups = [grouping_of(source, model, spec) for spec in group_by]
    for each in sums:
        if column(source, model, each).kind != NUMBER:
            raise ToolError(f"{each} : non sommable. Sommables : {numbers(source)}.")
    asked = order_of(source, model, order, [*groups, *sums])
    totals = [f"{each}:sum" for each in sums]
    with refused(f"query_data {model} grouped", f"Experio n'a pas pu lire {model} : {{reason}}"):
        found = odoo_call(model, "read_group", full, totals, groups, lazy=False, orderby=asked, limit=shown,
                          context=context)  # fmt: skip
    keep = {"__count", *groups, *sums, *(spec.split(":")[0] for spec in groups)}
    rows = [rounded({("count" if k == "__count" else k): v for k, v in row.items() if k in keep}) for row in found]
    return answered({"dossier": name, "model": model, "groups": rows})


def numbers(source):
    """Return the names of ``source``'s number columns, comma separated."""
    return ", ".join(name for name, col in source.columns.items() if col.kind == NUMBER)
