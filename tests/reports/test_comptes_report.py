"""The dossier accounts report: what it reads, the figures it sets in tables, the rules of its points d'attention, and
the gate every report goes through."""

import io
from datetime import date

import pytest
from docx import Document as WordDocument

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.data import query
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
    assert accounts.OPEN_NOTE in said and accounts.NO_OPENING_NOTE in said


@pytest.mark.functionality
def test_goods_bought_without_a_stock_variation_cannot_give_a_gross_margin():
    stockless = found_with(
        results=[account("61110000", "Achats", 60.0, 0.0), account("71110000", "Ventes", 0.0, 100.0)]
    )
    assert any("Aucune variation de stock" in point for point in points(stockless))
    assert not any("Aucune variation de stock" in point for point in points(found_with()))


@pytest.mark.functionality
def test_fixed_assets_without_a_depreciation_are_flagged():
    assets = [account("23310000", "Installations", 500.0, 0.0), account("28331000", "Amortissements", 0.0, 100.0)]
    assert any("dotation aux amortissements (6193)" in p for p in points(found_with(assets=assets)))
    booked = found_with(assets=assets, results=[*found_with()["results"], account("61930000", "Dotations", 40.0, 0.0)])
    assert not any("dotation aux amortissements" in p for p in points(booked))
    assert not any("dotation aux amortissements" in p for p in points(found_with()))


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


@pytest.mark.functionality
def test_clients_open_beyond_the_years_produits_are_flagged():
    said = points(found_with(clients=([third("ATLAS", 250.0)], 250.0)))
    assert any("Les clients ouverts (250.00) dépassent les produits de l'exercice (100.00)" in p for p in said)


@pytest.mark.functionality
def test_vat_collected_and_not_paid_is_flagged_only_when_the_account_is_in_credit():
    owed = found_with(vat=[account("44550000", "TVA facturée", 0.0, 4.0)])
    assert any("TVA facturée non reversée (4455) : 4.00" in p for p in points(owed))
    paid = found_with(vat=[account("44550000", "TVA facturée", 9.0, 4.0)])
    assert not any("TVA facturée" in p for p in points(paid))


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


@pytest.mark.communication
def test_a_part_with_no_figure_is_left_out_of_the_document():
    found = found_with(cash=[], vat=[], clients=([], 0.0), months_sales=[], months_charges=[])
    titles = [b["text"] for b in accounts.accounts_document(found, date(2026, 11, 9)).blocks if b["type"] == "heading"]
    assert titles == ["Le résultat à ce jour", "Fournisseurs", "Points d'attention"]


# --- Reading Experio, and the tool ---


class Experio:
    """Experio for the report: the gate, the exercice, and the rows each read answers by what it asks."""

    def __init__(self, monkeypatch, refusal=""):
        self.reads = []
        monkeypatch.setattr(query, "require_dossier", lambda dossier_id: "ALPHA")
        monkeypatch.setattr(query, "allowed", lambda session_id, dossier_id, name: refusal)
        monkeypatch.setattr(accounts, "exercice_of", lambda name, dossier_id, year, context: EXERCICE)
        monkeypatch.setattr(accounts, "has_opening", lambda *args: True)
        monkeypatch.setattr(accounts, "last_booked", lambda *args: "2026-05-20")
        monkeypatch.setattr(accounts, "totals", self.totals)

    def totals(self, dossier_id, exercice, context, extra, groupby, values=("debit", "credit"), **more):
        self.reads.append((extra, groupby, values, more, context))
        if groupby == ["partner_id"]:
            return [third("ATLAS", 5.0)]
        if groupby == ["date:month"]:
            return [month("mai 2026", "2026-05-01", 0.0, 1.0)]
        return [account("71110000", "Ventes", 0.0, 1.0)]


@pytest.mark.security
def test_the_report_reads_through_the_one_gate_and_the_dossiers_context(monkeypatch):
    experio = Experio(monkeypatch)
    accounts.read_accounts("s1", 7, 2026)
    assert len(experio.reads) == 8
    assert all(read[4]["allowed_company_ids"] == [7] and read[4]["skip_fiscal_year_filter"] for read in experio.reads)
    assert all(read[4].get("lang") == "fr_FR" for read in experio.reads if read[1] == ["date:month"])


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
