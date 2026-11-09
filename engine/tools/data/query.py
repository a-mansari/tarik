"""The agent's two general read tools: what can be read (describe_data) and the reading itself (query_data).

query_data is read only by construction: it can call search_read, search_count and read_group, nothing else exists in
this module. It never takes a model, field, operator, sort or grouping the registry (registry.py) does not list; the
dossier is asked for by id, proven (require_dossier), forced into every domain and into Experio's context, and the
conversation must be allowed to read it (allowed). Filters are plain [field, operator, value] triples, all of them
true together: no "or", no path through another model."""

import datetime
import json
import re

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.experio import in_dossier, odoo_call, refused, require_dossier
from engine.tools.core.queries import Q
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
    UNSET,
)
from engine.tools.dossiers.people import allowed_on, sender_of

DEFAULT_LIMIT, MAX_LIMIT = 50, 200  # rows shown; groups too
MAX_TERMS, MAX_LIST, MAX_TEXT = 12, 50, 80  # filters, values of an "in", characters of a text value
MAX_CHARS = 20000  # an answer longer than this is refused, never cut: a cut answer reads as the whole
GRANULARITIES = ("day", "week", "month", "quarter", "year")
ORDER = re.compile(r"(?P<name>[a-z_]+(:[a-z]+)?)( (?P<way>asc|desc))?")
DAY = re.compile(r"\d{4}-\d\d-\d\d")


def known(model):
    """Return model ``model``'s registry entry. Raises ToolError, listing the readable models, when there is none."""
    if not isinstance(model, str) or model not in MODELS:
        raise ToolError(f"Modèle « {model} » non lisible. Modèles lisibles : {', '.join(MODELS)}.")
    return MODELS[model]


def column(source, model, name):
    """Return the registry column ``name`` of ``source`` (the entry of ``model``). Raises ToolError for any other,
    a name that is not a text included."""
    if not isinstance(name, str) or name not in source.columns:
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
        if col.kind in UNSET:
            use.append("false = non renseigné")
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
    TEXT: (lambda value: isinstance(value, str) and len(value.strip()) > 0 and len(value) <= MAX_TEXT,
           f"un texte de 1 à {MAX_TEXT} caractères"),
}  # fmt: skip


def value_of(col, name, value):
    """Return ``value`` as the type column ``col`` (called ``name``) takes. Raises ToolError when it is not one, or,
    for a choice, not one of its values."""
    if value is False and col.kind in UNSET:  # not set: Experio's false
        return value
    if col.kind == CHOICE:
        if not isinstance(value, str) or value not in col.values:
            raise ToolError(f"{name} : {value!r} inconnu. Valeurs : {', '.join(col.values)}.")
        return value
    valid, expected = EXPECTED[col.kind]
    if not valid(value):
        raise ToolError(f"{name} : {expected} est attendu, pas {value!r}.")
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
    if operator != "in":
        if value is False and operator not in ("=", "!="):
            raise ToolError(f"{name} : « non renseigné » (false) se demande avec = ou !=.")
        return (name, operator, value_of(col, name, value))
    if not isinstance(value, list) or not 0 < len(value) <= MAX_LIST:
        raise ToolError(f"{name} : « in » prend une liste de 1 à {MAX_LIST} valeurs.")
    return (name, operator, [value_of(col, name, item) for item in value])


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
    spec = str(spec)
    name, _, granularity = spec.partition(":")
    col = column(source, model, name)
    if col.kind not in GROUPABLE:
        groupable = ", ".join(n for n, c in source.columns.items() if c.kind in GROUPABLE)
        raise ToolError(f"{name} : non regroupable. Regroupables : {groupable}.")
    if col.kind == DATE:
        if granularity not in GRANULARITIES:
            raise ToolError(
                f"{name} : une date se regroupe par période : {', '.join(GRANULARITIES)} (« {name}:month »)."
            )
    elif granularity:
        raise ToolError(f"{name} : seule une date se regroupe par période.")
    return spec


def order_of(order, allowed):
    """Return the sort ``order`` ("champ", "champ desc"; "" for none) checked against ``allowed`` (names, group
    specs, sums). Raises ToolError for anything else."""
    if not order:
        return ""
    found = ORDER.fullmatch(order.strip()) if isinstance(order, str) else None
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


def own_dossier(session_id, dossier_id):
    """Return whether a conversation without a sender (the chat, a routine's period) may read dossier ``dossier_id``:
    it is one the conversation worked in, or, when it worked in none, the first one it asks for, which it then keeps
    (tarik_session_dossiers). A conversation reads one dossier."""
    with cabinet_db() as db, db.transaction():
        worked = {row[0] for row in db.execute(Q.dossiers_of_session, (session_id,))}
        if worked:
            return dossier_id in worked
        db.execute(Q.bind_session_dossier, (session_id, dossier_id))
        return db.execute(Q.session_dossier, (session_id,)).fetchone()[0] == dossier_id


def allowed(session_id, dossier_id, name):
    """Return why the conversation ``session_id`` may not read dossier ``dossier_id`` named ``name``, "" when it may.

    An email or WhatsApp conversation is checked against its sender, as check_sender does: Experio staff, or a
    collaborator with the dossier. A conversation without a sender is limited to its own dossier (own_dossier)."""
    address = sender_of(session_id)
    if address is None:
        if own_dossier(session_id, dossier_id):
            return ""
        return "Cette conversation ne porte pas sur ce dossier : ouvrez-en une autre pour celui-ci."  # no name: a probe
    result = allowed_on(address, dossier_id, name)
    return result["message"] if result["status"] == "stop" else ""


def opened(dossier_id, session_id):
    """Return (the dossier's name, the context of every read in it) once dossier ``dossier_id`` is proven and the
    conversation ``session_id`` may read it: the one gate of every read of a dossier's data (query_data and the
    reports). Raises ToolError otherwise (require_dossier, allowed)."""
    name = require_dossier(dossier_id)
    refusal = allowed(session_id, dossier_id, name)
    if refusal:
        raise ToolError(refusal)
    # Experio's read_group alone keeps only the selected exercice's posted records (exp_account base.py:575-592 and
    # :332-357); a client asks about any year, as dossiers/progress.py counts.
    return name, {**in_dossier(dossier_id), "skip_fiscal_year_filter": True}


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
        dossier_id: the dossier (from find_dossier). Every read stays inside it; a conversation that has no sender
            (the chat) reads one dossier only.
        model: a readable model (describe_data).
        session_id: the conversation (filled in by Tarik).
        domain: filters, all true together, each [column, operator, value], e.g. [["state", "=", "post"],
            ["invoice_date", ">=", "2026-01-01"]]. Operators depend on the column's kind (describe_data); "in" takes
            a list; a date is AAAA-MM-JJ; a choice takes one of its values; false means not set (= or != only).
        fields: the columns to return (all when empty); records mode only.
        group_by: up to two columns to group by, a date by period ("invoice_date:month"); with it (or sums) the answer
            is one row per group with its count and sums.
        sums: number columns to total per group (grouped mode), e.g. ["amount_untaxed"].
        order: a returned column (or group, sum, or count in grouped mode), then asc or desc.
        limit: rows (or groups) shown, 1 to 200.

    Returns JSON: records mode {dossier, model, total, shown, rows}; grouped mode {dossier, model, groups, truncated}
    (truncated true: more groups exist than the limit shows).

    Raises:
        ToolError: the dossier is unknown or not the conversation's, the model, a column, an operator or a value is not
            allowed, or Experio can't answer."""
    source = known(model)
    name, context = opened(dossier_id, session_id)
    terms = terms_of(source, model, domain)
    shown = limit_of(limit)
    full = [("company_id", "=", dossier_id), *source.base, *terms]
    log(f"query_data dossier {dossier_id} {model} filters on {[term[0] for term in terms]}")
    if group_by or sums:
        return grouped(source, model, name, full, context, group_by or [], sums or [], order, shown)
    wanted = fields or list(source.columns)
    if not isinstance(wanted, list):
        raise ToolError("fields : une liste de champs est attendue.")
    for each in wanted:
        column(source, model, each)
    ordered = order_of(order, [n for n, c in source.columns.items() if c.kind in SORTABLE])
    with refused(f"query_data {model}", f"Experio n'a pas pu lire {model} : {{reason}}"):
        rows = odoo_call(
            model, "search_read", full, fields=wanted, order=ordered or source.order, limit=shown, context=context
        )
        # a page that is not full is everything there is
        total = len(rows) if len(rows) < shown else odoo_call(model, "search_count", full, context=context)
    return answered(
        {"dossier": name, "model": model, "total": total, "shown": len(rows), "rows": [rounded(row) for row in rows]}
    )


def grouped(source, model, name, full, context, group_by, sums, order, shown):
    """Return the answer of a grouped read: one row per group with its count and sums (JSON text), and whether more
    groups exist than ``shown``."""
    if not isinstance(group_by, list) or not isinstance(sums, list):
        raise ToolError("group_by et sums : des listes de champs sont attendues.")
    if not group_by or len(group_by) > 2:
        raise ToolError("group_by : un ou deux champs sont attendus (avec sums, au moins un).")
    groups = [grouping_of(source, model, spec) for spec in group_by]
    for each in sums:
        if column(source, model, each).kind != NUMBER:
            numbers = ", ".join(n for n, c in source.columns.items() if c.kind == NUMBER)
            raise ToolError(f"{each} : non sommable. Sommables : {numbers}.")
    asked = order_of(order, [*groups, *sums, "count"])
    asked = "__count" + asked[5:] if asked.split(" ")[0] == "count" else asked
    totals = [f"{each}:sum" for each in sums]
    with refused(f"query_data {model} grouped", f"Experio n'a pas pu lire {model} : {{reason}}"):
        found = odoo_call(
            model, "read_group", full, totals, groups, lazy=False, orderby=asked, limit=shown + 1, context=context
        )
    keep = {"__count", *groups, *sums, *(spec.split(":")[0] for spec in groups)}
    rows = [rounded({("count" if k == "__count" else k): v for k, v in row.items() if k in keep}) for row in found]
    return answered({"dossier": name, "model": model, "groups": rows[:shown], "truncated": len(rows) > shown})
