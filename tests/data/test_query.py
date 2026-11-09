"""The agent's general read tools: what they refuse, what they always add, and that they only ever read."""

import json

import pytest

from engine import agent
from engine.tools.core.app import ToolError
from engine.tools.data import query, registry
from engine.tools.data.query import describe_data, query_data

DOSSIER = 5
ALLOWED = {"status": "ok", "message": "ok"}


class Experio:
    """A fake Experio: records every call, answers rows to a read and a count to a count."""

    def __init__(self, monkeypatch, rows=None, allowed=ALLOWED):
        self.calls = []
        self.rows = rows if rows is not None else []
        monkeypatch.setattr(query, "require_dossier", lambda dossier_id: "ALPHA")
        monkeypatch.setattr(query, "check_sender", lambda session_id, dossier_id: allowed)
        monkeypatch.setattr(query, "odoo_call", self.call)

    def call(self, model, method, domain, *more, **kw):
        self.calls.append((model, method, domain, kw))
        return len(self.rows) if method == "search_count" else self.rows


@pytest.fixture
def experio(monkeypatch):
    return Experio(monkeypatch)


def ask(**arguments):
    return query_data(DOSSIER, arguments.pop("model", "exp.invoice"), "s1", **arguments)


# --- Isolation: the dossier is forced, whatever the agent asks ---


@pytest.mark.security
def test_every_read_is_pinned_to_the_dossier_in_its_domain_and_context(experio):
    ask(domain=[["state", "=", "post"]], model="exp.invoice")
    ask(model="exp.move.line", group_by=["account_id"], sums=["debit"])
    for _model, _method, domain, kw in experio.calls:
        assert domain[0] == ("company_id", "=", DOSSIER)
        assert kw["context"] == {"allowed_company_ids": [DOSSIER]}


@pytest.mark.security
def test_the_dossier_cannot_be_asked_for_by_a_filter(experio):
    for wanted in (["company_id", "=", 6], ["company_id", "in", [5, 6]]):
        with pytest.raises(ToolError, match="non lisible"):
            ask(domain=[wanted])
    assert experio.calls == []


@pytest.mark.security
def test_a_model_keeps_its_base_terms_whatever_the_filters(experio):
    ask(model="exp.move.line", domain=[["reconciled", "=", False]])
    domain = experio.calls[0][2]
    assert ("parent_state", "in", ["posted", "exported"]) in domain
    assert ("exp_deleted", "=", False) in domain
    assert ("move_id.exp_deleted", "=", False) in domain


@pytest.mark.security
def test_only_a_read_is_ever_made(experio):
    ask()
    ask(model="exp.move.line", group_by=["journal_id"], sums=["credit"])
    ask(model="exp.account", fields=["code"], order="code desc")
    assert {method for _, method, _, _ in experio.calls} <= {"search_read", "search_count", "read_group"}


@pytest.mark.security
def test_a_sender_who_may_not_work_on_the_dossier_reads_nothing(monkeypatch):
    experio = Experio(monkeypatch, allowed={"status": "stop", "message": "Le dossier n'est pas à vous."})
    with pytest.raises(ToolError, match="pas à vous"):
        ask()
    assert experio.calls == []


@pytest.mark.security
def test_the_agent_cannot_set_the_conversation_or_the_cabinet():
    arguments = set(next(t for t in agent.TOOLS if t.name == "Experio_query_data").args)
    assert arguments == {"dossier_id", "model", "domain", "fields", "group_by", "sums", "order", "limit"}


# --- What is never readable ---


@pytest.mark.security
@pytest.mark.parametrize("model", ["res.users", "ir.config_parameter", "res.partner.bank", "exp.invoice.line", ""])
def test_a_model_the_registry_does_not_list_is_refused(experio, model):
    with pytest.raises(ToolError, match="non lisible"):
        ask(model=model)
    assert experio.calls == []


@pytest.mark.security
@pytest.mark.parametrize(
    "domain",
    [
        [["partner_id.bank_ids.acc_number", "ilike", "1"]],  # a path into a model the registry does not list
        [["company_id.name", "=", "x"]],
        [["create_uid", "=", 1]],
        [["message_ids", "in", [1]]],
    ],
)
def test_a_filter_on_a_column_or_path_not_listed_is_refused(experio, domain):
    with pytest.raises(ToolError, match="non lisible"):
        ask(domain=domain)


@pytest.mark.security
def test_a_field_sort_or_group_not_listed_is_refused(experio):
    with pytest.raises(ToolError, match="non lisible"):
        ask(fields=["id", "attachment_ids"])
    with pytest.raises(ToolError, match="refusé"):
        ask(order="create_date desc")
    with pytest.raises(ToolError, match="non lisible"):
        ask(group_by=["create_uid"], sums=["amount_total"])


@pytest.mark.security
def test_a_link_cannot_be_sorted_on_it_sorts_by_another_models_name(experio):
    with pytest.raises(ToolError, match="refusé"):
        ask(order="partner_id")


@pytest.mark.security
def test_the_registry_lists_nothing_sensitive():
    names = {name for source in registry.MODELS.values() for name in source.columns}
    for word in (
        "password",
        "login",
        "key",
        "token",
        "iban",
        "rib",
        "acc_number",
        "salary",
        "attachment",
        "company_id",
    ):
        assert not [name for name in names if word in name]
    assert not [model for model in registry.MODELS if model.startswith(("res.users", "ir.", "res.partner.bank"))]


# --- Filters ---


@pytest.mark.functionality
def test_a_valid_filter_reaches_experio_as_a_term(experio):
    ask(domain=[["state", "in", ["draft", "post"]], ["amount_total", ">=", 100], ["invoice_date", "<", "2027-01-01"]])
    domain = experio.calls[0][2]
    assert ("state", "in", ["draft", "post"]) in domain
    assert ("amount_total", ">=", 100) in domain
    assert ("invoice_date", "<", "2027-01-01") in domain


@pytest.mark.security
@pytest.mark.parametrize(
    "domain, match",
    [
        ([["state", "like", "p%"]], "opérateur"),
        ([["amount_total", "ilike", "1"]], "opérateur"),
        ([["state", "=", "paid"]], "inconnu"),
        (
            [
                ["state", "in", ["draft", "nope"]],
            ],
            "inconnu",
        ),
        ([["invoice_date", "=", "2026-13-40"]], "date"),
        ([["invoice_date", "=", "hier"]], "date"),
        ([["amount_total", ">", "100"]], "nombre"),
        ([["amount_total", ">", True]], "nombre"),
        ([["partner_id", "=", "ATLAS"]], "numéro"),
        ([["partner_id", "=", -1]], "numéro"),
        ([["refund", "=", "oui"]], "vrai ou faux"),
        ([["partner_name", "ilike", ""]], "texte"),
        ([["partner_name", "ilike", "x" * 200]], "texte"),
        ([["partner_id", "in", list(range(1, 100))]], "liste"),
        ([["partner_id", "in", 5]], "liste"),
        ([["state", "="]], "forme"),
        ([["|", "state", "="]], "non lisible"),
        ([["state", "=", "draft"]] * 13, "au plus"),
        ("state = draft", "au plus"),
    ],
)
def test_a_filter_of_the_wrong_shape_operator_or_value_is_refused(experio, domain, match):
    with pytest.raises(ToolError, match=match):
        ask(domain=domain)
    assert experio.calls == []


# --- Records and groups ---


@pytest.mark.functionality
def test_records_come_with_their_total_and_the_dossier(monkeypatch):
    Experio(monkeypatch, rows=[{"id": 1, "amount_total": 10.005000001}])
    answer = json.loads(ask(limit=500))
    assert answer == {"dossier": "ALPHA", "model": "exp.invoice", "total": 1, "shown": 1,
                      "rows": [{"id": 1, "amount_total": 10.01}]}  # fmt: skip


@pytest.mark.functionality
def test_the_limit_is_capped_and_a_bad_one_refused(experio):
    ask(limit=10_000)
    assert experio.calls[0][3]["limit"] == query.MAX_LIMIT
    for bad in (0, -1, "10", True):
        with pytest.raises(ToolError, match="limit"):
            ask(limit=bad)


@pytest.mark.functionality
def test_a_grouped_read_groups_sums_and_counts(monkeypatch):
    found = [{"invoice_date:month": "janvier 2026", "type": "sale", "__count": 3, "amount_untaxed": 100.0,
              "__domain": ["secret"], "__range": {}}]  # fmt: skip
    experio = Experio(monkeypatch, rows=found)
    answer = json.loads(
        ask(group_by=["invoice_date:month", "type"], sums=["amount_untaxed"], order="amount_untaxed desc")
    )
    model, method, domain, kw = experio.calls[0]
    assert method == "read_group" and kw["lazy"] is False and kw["orderby"] == "amount_untaxed desc"
    assert domain and kw["context"] == {"allowed_company_ids": [DOSSIER]}
    assert answer["groups"] == [
        {"invoice_date:month": "janvier 2026", "type": "sale", "count": 3, "amount_untaxed": 100.0}
    ]


@pytest.mark.security
@pytest.mark.parametrize(
    "arguments, match",
    [
        ({"group_by": ["invoice_date"], "sums": []}, "période"),
        ({"group_by": ["invoice_date:decade"], "sums": []}, "période"),
        ({"group_by": ["type:month"], "sums": []}, "période"),
        ({"group_by": ["invoice_number"], "sums": []}, "non regroupable"),
        ({"group_by": ["type"], "sums": ["state"]}, "non sommable"),
        ({"group_by": ["type"], "sums": ["amount_total"], "order": "state"}, "refusé"),
        ({"group_by": ["type", "state", "payment_state"], "sums": []}, "un ou deux"),
        ({"group_by": [], "sums": ["amount_total"]}, "un ou deux"),
    ],
)
def test_a_grouping_or_sum_the_registry_does_not_allow_is_refused(experio, arguments, match):
    with pytest.raises(ToolError, match=match):
        ask(**arguments)
    assert experio.calls == []


@pytest.mark.technical
def test_an_answer_too_long_is_refused_never_cut(monkeypatch):
    Experio(monkeypatch, rows=[{"id": 1, "partner_name": "x" * 50_000}])
    with pytest.raises(ToolError, match="trop long"):
        ask()


@pytest.mark.communication
def test_an_experio_failure_reaches_the_agent_as_a_refusal(monkeypatch):
    Experio(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("secret traceback")

    monkeypatch.setattr(query, "odoo_call", broken)
    with pytest.raises(ToolError) as refused:
        ask()
    assert "secret" not in str(refused.value)


# --- describe_data ---


@pytest.mark.communication
def test_describe_data_lists_the_models_then_one_models_columns():
    listed = describe_data()
    assert all(model in listed for model in registry.MODELS)
    detail = describe_data("exp.invoice")
    assert "state [choice]" in detail and "post = comptabilisée" in detail and "amount_total [number]" in detail
    assert "company_id" not in detail
    with pytest.raises(ToolError, match="non lisible"):
        describe_data("res.users")


@pytest.mark.technical
def test_the_registry_is_well_formed():
    for source in registry.MODELS.values():
        assert "id" in source.columns and source.order
        for col in source.columns.values():
            assert col.kind in registry.OPERATORS and col.label
            assert bool(col.values) == (col.kind == registry.CHOICE)
