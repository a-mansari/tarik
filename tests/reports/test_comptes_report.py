"""The dossier accounts report: what it reads, the figures it sets in tables, the rules of its points d'attention, and
the gate every report goes through."""

import io
from datetime import date

import pytest
from docx import Document as WordDocument

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.data import query, reports
from engine.tools.reports import accounts, status

EXERCICE = {"name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31", "state": "open"}


def account(code, label, debit, credit):
    return {"account_id": [1, f"{code} {label}"], "debit": debit, "credit": credit}


def month(label, start, debit, credit):
    return {"date:month": label, "debit": debit, "credit": credit, "__range": {"date:month": {"from": start}}}


def third(name, residual):
    return {"partner_id": [1, name], "amount_residual": residual}


def found_with(**more):
    """A dossier whose books show nothing to flag, then ``more`` changed."""
    base = {
        "name": "ALPHA",
        "exercice": {**EXERCICE, "state": "shut"},
        "opening": True,
        "last": "2026-05-20",
        "results": [
            account("61110000", "Achats", 60.0, 0.0),
            account("71110000", "Ventes", 0.0, 100.0),
            account("61140000", "Variation de stock", 5.0, 0.0),
        ],  # fmt: skip
        "whole": True,
        "months_sales": [month("mai 2026", "2026-05-01", 0.0, 100.0)],
        "months_charges": [month("mai 2026", "2026-05-01", 60.0, 0.0)],
        "cash": [account("51410100", "Banque", 10.0, 2.0)],
        "clients": ([third("ATLAS", 30.0)], 30.0),
        "suppliers": ([third("MAROC", -20.0)], -20.0),
        "vat": [account("44550000", "TVA facturée", 4.0, 4.0)],
        "assets": [],
    }
    return {**base, **more}


def points(found):
    table, totals = accounts.result_table(found)
    return accounts.attention(found, totals)


@pytest.mark.functionality
def test_the_result_is_set_by_rubrique_with_the_gap_said_in_words():
    table, totals = accounts.result_table(found_with())
    assert totals == (65.0, 100.0)
    assert table["rows"][:2] == [
        ["61 Charges d'exploitation", "65.00", ""],
        ["71 Produits d'exploitation", "", "100.00"],
    ]
    assert table["rows"][-2:] == [
        ["Total", "65.00", "100.00"],
        ["Écart produits moins charges, avant écritures de clôture", "", "excédent 35.00"],
    ]
    loss = accounts.result_table(found_with(results=[account("61110000", "Achats", 9.0, 0.0)]))[0]
    assert loss["rows"][-1][2] == "déficit 9.00"


@pytest.mark.functionality
def test_a_credit_on_a_charge_account_reduces_the_charges():
    found = found_with(results=[account("61110000", "Achats", 60.0, 20.0)])
    assert accounts.result_table(found)[1] == (40.0, 0.0)


@pytest.mark.technical
def test_a_plan_too_long_to_read_gives_no_result_and_no_rule_built_on_it():
    found = found_with(whole=False, exercice=EXERCICE)
    assert accounts.result_table(found) == (None, None)
    said = points(found)
    assert not any("Aucune variation de stock" in point or "charges dépassent" in point for point in said)
    document = accounts.accounts_document(found, date(2026, 11, 9))
    assert "le résultat n'est pas donné" in document.blocks[1]["text"]


@pytest.mark.functionality
def test_a_dossier_with_nothing_to_flag_says_so():
    assert points(found_with()) == ["Rien à signaler dans ces chiffres."]


@pytest.mark.functionality
def test_an_exercice_not_closed_and_one_without_opening_entries_are_flagged():
    said = points(found_with(exercice=EXERCICE, opening=False))
    assert reports.OPEN_NOTE in said and reports.NO_OPENING_NOTE in said and reports.OPENING_NOTE not in said


@pytest.mark.functionality
def test_goods_bought_without_a_stock_variation_cannot_give_a_gross_margin():
    stockless = found_with(
        results=[account("61110000", "Achats", 60.0, 0.0), account("71110000", "Ventes", 0.0, 100.0)]
    )
    assert any("Aucune variation de stock" in point for point in points(stockless))
    assert not any("Aucune variation de stock" in point for point in points(found_with()))


@pytest.mark.functionality
def test_depreciable_assets_without_any_dotation_are_flagged():
    assets = [account("23310000", "Installations", 500.0, 0.0), account("28331000", "Amortissements", 0.0, 100.0)]
    assert any("dotation aux amortissements (6191 à 6193)" in p for p in points(found_with(assets=assets)))
    assert not any("dotation aux amortissements" in p for p in points(found_with()))
    for code in ("61910000", "61920000", "61930000"):  # Experio books its dotations on 6191, 6192 and 6193
        booked = found_with(assets=assets, results=[*found_with()["results"], account(code, "Dotations", 40.0, 0.0)])
        assert not any("dotation aux amortissements" in p for p in points(booked)), code


@pytest.mark.functionality
def test_only_the_classes_that_depreciate_are_asked_about_a_dotation():
    financial = [account("24860000", "Dépôts et cautionnements", 500.0, 0.0)]  # never depreciated
    assert not any("dotation aux amortissements" in p for p in points(found_with(assets=financial)))
    non_valeurs = [account("21110000", "Frais préliminaires", 500.0, 0.0)]
    assert any("dotation aux amortissements" in p for p in points(found_with(assets=non_valeurs)))
    assert accounts.FIXED == ("21", "22", "23")


@pytest.mark.functionality
def test_sales_that_stop_before_the_last_entry_are_flagged_by_the_date_not_the_label():
    stopped = found_with(last="2026-08-09")
    assert any(
        "Aucune vente (71) n'est comptabilisée après mai 2026" in p and "09/08/2026" in p for p in points(stopped)
    )
    assert not any("Aucune vente" in p for p in points(found_with()))
    assert not any("Aucune vente" in p for p in points(found_with(months_sales=[], last="2026-08-09")))


@pytest.mark.functionality
def test_months_come_in_date_order_whatever_their_labels():
    found = found_with(
        months_sales=[month("avril 2026", "2026-04-01", 0.0, 1.0), month("mars 2026", "2026-03-01", 0.0, 2.0)],
        months_charges=[month("février 2026", "2026-02-01", 3.0, 0.0), month("mars 2026", "2026-03-01", 4.0, 0.0)],
    )
    assert [m["label"] for m in accounts.by_month(found)] == ["février 2026", "mars 2026", "avril 2026"]
    rows = accounts.months_table(found)["rows"]
    assert rows[0] == ["février 2026", "", "3.00"] and rows[1] == ["mars 2026", "2.00", "4.00"]


@pytest.mark.functionality
def test_a_bank_account_in_credit_with_clients_open_asks_for_the_bank_reconciliation():
    said = points(found_with(cash=[account("51410100", "Banque", 1.0, 9.0), account("51610000", "Caisse", 0.0, 3.0)]))
    bank = next(p for p in said if p.startswith("Des comptes bancaires sont créditeurs"))
    assert "51410100 Banque (8.00)" in bank and "Caisse" not in bank and "rapprochement bancaire" in bank
    nobody = found_with(cash=[account("51410100", "Banque", 1.0, 9.0)], clients=([], 0.0))
    assert "À vérifier." in next(p for p in points(nobody) if p.startswith("Des comptes bancaires"))


@pytest.mark.functionality
def test_the_bank_hint_rests_on_the_total_of_all_the_clients_not_the_few_listed():
    found = found_with(cash=[account("51410100", "Banque", 1.0, 9.0)], clients=([third("PETIT", 1.0)], 900.0))
    assert any("rapprochement bancaire" in p for p in points(found))


@pytest.mark.functionality
def test_clients_open_with_the_vat_are_never_compared_with_the_sales_without_it():
    said = points(found_with(clients=([third("ATLAS", 250.0)], 250.0)))
    assert not any("dépassent les produits" in p for p in said)


@pytest.mark.functionality
def test_the_vat_due_is_the_collected_minus_the_recoverable_as_experios_table_does():
    owed = found_with(
        vat=[account("44550000", "TVA facturée", 0.0, 10.0), account("34552000", "TVA récupérable", 3.0, 0.0)]
    )
    assert any("TVA à payer selon les comptes (4455 moins 3455) : 7.00" in p for p in points(owed))
    even = found_with(
        vat=[account("44550000", "TVA facturée", 0.0, 10.0), account("34552000", "TVA récupérable", 10.0, 0.0)]
    )
    assert not any("TVA à payer" in p for p in points(even))
    credit = found_with(
        vat=[account("44550000", "TVA facturée", 0.0, 2.0), account("34552000", "TVA récupérable", 9.0, 0.0)]
    )
    assert not any("TVA à payer" in p for p in points(credit))


@pytest.mark.functionality
def test_the_vat_declared_and_not_paid_sits_on_4456():
    said = points(found_with(vat=[account("44560000", "TVA due", 0.0, 12.0)]))
    assert "TVA déclarée restant à payer (4456) : 12.00." in said


@pytest.mark.functionality
def test_a_month_whose_sales_net_to_zero_is_no_sales_month():
    found = found_with(
        months_sales=[month("février 2026", "2026-02-01", 0.0, 5.0), month("mars 2026", "2026-03-01", 4.0, 4.0)],
        last="2026-05-20",
    )
    assert any("après février 2026" in p for p in points(found))


@pytest.mark.communication
def test_the_document_sets_every_part_in_a_table_and_says_where_the_figures_come_from():
    document = accounts.accounts_document(found_with(), date(2026, 11, 9))
    titles = [b["text"] for b in document.blocks if b["type"] == "heading"]
    assert titles == [
        "Le résultat à ce jour",
        "Activité",
        "Trésorerie",
        "Clients",
        "Fournisseurs",
        "TVA",
        "Points d'attention",
    ]
    note = document.blocks[-1]["text"]
    assert "Chiffres lus dans Experio le 09/11/2026 ; écritures jusqu'au 20/05/2026" in note
    assert "lecture automatique" in note and "ni un bilan ni un conseil" in note
    assert document.title == "Comptes du dossier ALPHA" and "Exercice 2026" in document.subtitle
    clients = next(b for b in document.blocks if b.get("caption", "").startswith("Clients"))
    assert clients["rows"][-1] == ["Total de tous les tiers", "débiteur 30.00"]
    assert clients["caption"] == "Clients : plus gros soldes ouverts (342)"


@pytest.mark.communication
def test_a_part_with_no_figure_is_left_out_of_the_document():
    found = found_with(cash=[], vat=[], clients=([], 0.0), months_sales=[], months_charges=[])
    titles = [b["text"] for b in accounts.accounts_document(found, date(2026, 11, 9)).blocks if b["type"] == "heading"]
    assert titles == ["Le résultat à ce jour", "Fournisseurs", "Points d'attention"]


# --- Reading Experio, and the tool ---


class Experio:
    """Experio for the report: the gate, the exercice, and the rows each read answers by what it asks."""

    def __init__(self, monkeypatch, refusal="", opening=True, partners=1, results=None):
        self.reads = []
        self.partners, self.results = partners, results
        monkeypatch.setattr(query, "require_dossier", lambda dossier_id: "ALPHA")
        monkeypatch.setattr(query, "allowed", lambda session_id, dossier_id, name: refusal)
        monkeypatch.setattr(accounts, "exercice_of", lambda name, dossier_id, year, context: EXERCICE)
        monkeypatch.setattr(accounts, "has_opening", lambda *args: opening)
        monkeypatch.setattr(accounts, "last_booked", lambda *args: "2026-05-20")
        monkeypatch.setattr(accounts, "totals", self.totals)
        monkeypatch.setattr(reports, "totals", self.totals)

    def totals(self, dossier_id, exercice, context, extra, groupby, values=("debit", "credit"), **more):
        self.reads.append((extra, groupby, values, more, context))
        if values == ("amount_residual",) and groupby == []:
            return [{"amount_residual": 900.0}]  # the whole, apart from the biggest listed
        if values == ("amount_residual",):
            return [third(f"T{n}", 100.0 - n) for n in range(min(self.partners, more.get("limit", 151)))]
        if groupby == ["date:month"]:
            return [month("mai 2026", "2026-05-01", 0.0, 1.0)]
        return self.results if self.results is not None else [account("71110000", "Ventes", 0.0, 1.0)]


@pytest.mark.security
def test_the_report_reads_through_the_one_gate_and_the_dossiers_context(monkeypatch):
    experio = Experio(monkeypatch)
    accounts.read_accounts("s1", 7, 2026)
    assert len(experio.reads) == 10
    assert all(read[4]["allowed_company_ids"] == [7] and read[4]["skip_fiscal_year_filter"] for read in experio.reads)
    assert all(read[4].get("lang") == "fr_FR" for read in experio.reads if read[1] == ["date:month"])


def read_of(experio, prefix):
    """The read of the accounts starting with ``prefix`` (the one whose terms name it), None when there is none."""
    term = ("account_id.code", "=like", f"{prefix}%")
    return next((read for read in experio.reads if term in read[0] and read[1] == ["account_id"]), None)


@pytest.mark.functionality
def test_the_balance_sheet_accounts_are_read_from_the_origin_when_there_is_no_opening(monkeypatch):
    without = Experio(monkeypatch, opening=False)
    accounts.read_accounts("s1", 7, 2026)
    for prefix in ("51", "3455", "21"):
        assert read_of(without, prefix)[3]["since_origin"] is True, prefix
    assert read_of(without, "6")[3].get("since_origin") is None  # the result and the months: movements of the exercice
    assert all("since_origin" not in read[3] for read in without.reads if read[1] == ["date:month"])
    with_opening = Experio(monkeypatch, opening=True)
    accounts.read_accounts("s1", 7, 2026)
    assert read_of(with_opening, "51")[3]["since_origin"] is False


@pytest.mark.functionality
def test_the_biggest_third_parties_are_read_by_amount_and_the_total_apart_so_a_long_list_is_never_cut_by_name(
    monkeypatch,
):
    experio = Experio(monkeypatch, partners=500)
    found = accounts.read_accounts("s1", 7, 2026)
    clients = [read for read in experio.reads if read[2] == ("amount_residual",) and read[1] == ["partner_id"]]
    assert clients[0][3]["orderby"] == "amount_residual desc" and clients[0][3]["limit"] == accounts.TOP
    assert clients[1][3]["orderby"] == "amount_residual asc"  # the suppliers: the biggest creditors first
    rows, total = found["clients"]
    assert len(rows) == accounts.TOP and total == 900.0
    assert any(read[1] == [] and read[3]["limit"] == 1 for read in experio.reads)  # the total of all, one row


@pytest.mark.functionality
def test_a_plan_of_exactly_the_limit_is_read_as_too_long_and_the_assets_are_then_not_read(monkeypatch):
    many = [account(f"6{n:07d}", "Compte", 1.0, 0.0) for n in range(accounts.ALL)]
    experio = Experio(monkeypatch, results=many)
    found = accounts.read_accounts("s1", 7, 2026)
    assert found["whole"] is False and found["assets"] == [] and read_of(experio, "21") is None
    assert accounts.result_table(found) == (None, None)
    Experio(monkeypatch, results=many[:-1])
    assert accounts.read_accounts("s1", 7, 2026)["whole"] is True


@pytest.mark.security
def test_a_conversation_that_may_not_read_the_dossier_gets_no_report_and_nothing_is_read(monkeypatch):
    experio = Experio(monkeypatch, refusal="Pas votre dossier.")
    with pytest.raises(ToolError, match="Pas votre dossier"):
        accounts.read_accounts("s1", 7, 2026)
    assert experio.reads == []


def outbox(session):
    with cabinet_db() as db:
        return db.execute(
            "select file_name, data from tarik_outbox where session_id = %s order by id", (session,)
        ).fetchall()


@pytest.mark.functionality
def test_the_tool_makes_the_accounts_report_in_the_formats_asked(db_cabinet, monkeypatch):
    Experio(monkeypatch)
    text = status.make_report("s1", "comptes", 7, ["docx", "pdf"], 2026, "")
    kept = outbox("s1")
    assert [name for name, _ in kept] == ["comptes-du-dossier-alpha.docx", "comptes-du-dossier-alpha.pdf"]
    assert text.startswith("Fichiers prêts : comptes-du-dossier-alpha.docx")
    word = WordDocument(io.BytesIO(bytes(kept[0][1])))
    assert word.core_properties.title == "Comptes du dossier ALPHA"
    assert "Points d'attention" in [p.text for p in word.paragraphs]


@pytest.mark.security
def test_the_tool_refuses_before_reading_and_leaves_no_file_for_a_conversation_not_allowed(db_cabinet, monkeypatch):
    experio = Experio(monkeypatch, refusal="Pas votre dossier.")
    with pytest.raises(ToolError, match="Pas votre dossier"):
        status.make_report("s1", "comptes", 7, ["docx"], 2026)
    assert experio.reads == [] and outbox("s1") == []


@pytest.mark.communication
def test_the_two_kinds_are_listed_in_a_refusal(db_cabinet, monkeypatch):
    Experio(monkeypatch)
    with pytest.raises(ToolError, match="statut, comptes"):
        status.make_report("s1", "bilan", 7, ["docx"])
