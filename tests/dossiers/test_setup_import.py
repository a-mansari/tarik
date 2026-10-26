"""A dossier's configuration copied from a model, and its files through Experio's Importation screen (db_cabinet):
tested by Experio first, imported only when its test finds nothing, every refusal a stop with the file taken."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.dossiers import setup

FEC = "JournalCode;EcritureDate;CompteNum;Debit;Credit\nAC;20250101;6111;10;0"
BANKS = [{"id": 3, "name": "CREDIT IMMOBILIER ET HOTELIER (CIH)"}, {"id": 4, "name": "ATTIJARIWAFA BANK"}]


@pytest.fixture
def copying(monkeypatch):
    """Dossier 557 to copy model 890 into, without entries; Experio's calls."""
    calls = []
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: {557: "Gecafisc", 890: "MODELE"}[dossier_id])
    monkeypatch.setattr(setup, "dry_run", lambda: False)
    monkeypatch.setattr(setup, "odoo_call", lambda model, method, *a, **k: 0)
    monkeypatch.setattr(setup, "make_advance_accounts_reconcilable", lambda model_id: calls.append("reconcilable"))
    monkeypatch.setattr(setup, "odoo_json", lambda model, method, arg: calls.append((method, arg)) or 31)
    return calls


@pytest.mark.functionality
def test_the_models_configuration_is_copied_through_experios_wizard(copying):
    answer = setup.copy_configuration(557, 890)
    assert answer["message"].startswith("Configuration du dossier `MODELE` copiee dans `Gecafisc`")
    assert copying[0] == "reconcilable" and copying[2] == ("action_apply", [31])
    wizard = copying[1][1]
    assert wizard["source_company_id"] == 890 and wizard["target_company_ids"] == [(6, 0, [557])]
    assert all(wizard[f"apply_{part}"] for part in ("accounts", "taxes", "journals", "configuration"))


@pytest.mark.functionality
def test_a_dossier_is_never_its_own_model(copying):
    with pytest.raises(ToolError, match="un autre dossier"):
        setup.copy_configuration(557, 557)
    assert copying == []


@pytest.mark.functionality
def test_no_copy_over_entries_already_imported(copying, monkeypatch):
    monkeypatch.setattr(setup, "odoo_call", lambda model, method, domain: 40)
    with pytest.raises(ToolError, match="`Gecafisc` a deja des ecritures importees"):
        setup.copy_configuration(557, 890)
    assert copying == []


@pytest.mark.security
def test_the_copy_is_only_said_in_test_mode(copying, monkeypatch):
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    assert setup.copy_configuration(557, 890)["message"].startswith(TEST_MODE) and copying == []


@pytest.mark.communication
def test_experio_refusing_the_copy_gives_its_reason(copying, monkeypatch):
    monkeypatch.setattr(setup, "odoo_json", lambda *a: (_ for _ in ()).throw(RuntimeError("compte 4421 manquant")))
    with pytest.raises(ToolError, match="Experio n'a pas pu copier la configuration : compte 4421 manquant"):
        setup.copy_configuration(557, 890)


@pytest.fixture
def importing(db_cabinet, monkeypatch):
    """Dossier 557 with Experio's import screen: its preview, its test and its import answers; its calls."""
    state = {
        "calls": [],
        "preview": {
            "headers": ["JournalCode", "Numero de piece", "Debit"],
            "matches": {"0": ["journal_code", "journal_id"], "2": ["debit"]},
            "options": {"separator": ";"},
        },
        "test": {"ids": [1, 2]},
        "result": {"ids": [1, 2, 3]},
        "counts": {"exp.account": 10, "exp.journal": 2, "exp.partner": 0, "exp.fiscal.entries": 1},
    }
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {"allowed_company_ids": [557]})
    monkeypatch.setattr(setup, "dry_run", lambda: False)

    def odoo_json(model, method, *args, context=None):
        state["calls"].append((model, method, args, context))
        if isinstance(state.get(method), Exception):
            raise state[method]
        if method == "create":
            return 5
        if method == "parse_preview":
            return state["preview"]
        if args[-1]:  # execute_import's dry run: Experio's test
            return state["test"]
        state["counts"]["exp.account"] += 3
        return state["result"]

    def odoo_call(model, method, *args, **kw):
        return BANKS if model == "res.bank" else state["counts"][model]

    monkeypatch.setattr(setup, "odoo_json", odoo_json)
    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    return state


def sent(name, text, mime="text/csv"):
    """A file the client sent in conversation s: its id."""
    data = text if isinstance(text, bytes) else text.encode()
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) values ('s', %s, %s, %s) returning id",
            (name, mime, data),
        ).fetchone()[0]


def kind_of(file_id):
    with cabinet_db() as db:
        return db.execute("select kind from tarik_files where id = %s", (file_id,)).fetchone()[0]


def imports(state):
    """execute_import's calls: (dry run or not, fields, options, context)."""
    return [(a[-1], a[1], a[3], c) for model, method, a, c in state["calls"] if method == "execute_import"]


@pytest.mark.functionality
def test_a_fec_is_tested_by_experio_then_imported_and_the_dossier_waits(importing):
    file_id = sent("fec.csv", FEC)
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "ok" and answer["target"] == "ecritures" and answer["file_id"] == file_id
    assert answer["message"] == (
        "FEC `fec.csv` importe dans le dossier `Gecafisc` : 3 lignes. Experio a cree : 3 compte(s)."
    )
    upload = importing["calls"][0]
    assert upload[1] == "create" and upload[2][0]["res_model"] == "experio.history" and upload[2][0]["file"] == FEC
    (test, *_), (real, fields, options, context) = imports(importing)
    assert test is True and real is False and context == {"allowed_company_ids": [557]}
    assert fields == ["journal_code", "reference", "debit"]  # the piece number mapped by hand, as a user does
    assert options["separator"] == ";" and options["order_lines"] is False
    assert setup.waiting_setups("s")["dossiers"] == [557] and kind_of(file_id) == "entries"


@pytest.mark.functionality
def test_a_grand_livre_is_imported_with_its_lines_sorted_by_reference(importing):
    answer = setup.import_entries(557, sent("gl.csv", FEC), kind="grand livre")
    assert answer["message"].startswith("Grand livre `gl.csv` importe")
    options = imports(importing)[-1][2]
    assert options["order_lines"] is True and options["order_lines_with_reference"] is True


@pytest.mark.functionality
def test_nothing_is_imported_when_experios_test_finds_errors(importing):
    importing["test"] = {"messages": [{"type": "error", "message": f"Compte {n} inconnu"} for n in range(4)]}
    file_id = sent("fec.csv", FEC)
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "stop" and answer["message"] == (
        "FEC `fec.csv` refuse par le test Experio (4 erreur(s)) : "
        "Compte 0 inconnu | Compte 1 inconnu | Compte 2 inconnu"
    )
    assert [dry for dry, *_ in imports(importing)] == [True]
    assert setup.waiting_setups("s")["dossiers"] == [] and kind_of(file_id) == "entries"


@pytest.mark.functionality
def test_errors_in_experios_import_itself_are_a_stop(importing):
    importing["result"] = {"messages": [{"type": "warning", "message": "lent"}, {"type": "error", "message": "x"}]}
    answer = setup.import_entries(557, sent("fec.csv", FEC))
    assert answer["status"] == "stop" and answer["message"] == "FEC `fec.csv` refuse par Experio : x"
    assert setup.waiting_setups("s")["dossiers"] == []


@pytest.mark.communication
@pytest.mark.parametrize(
    "failing, said",
    [
        ("create", "`fec.csv` : Experio n'a pas pu lire le fichier (boom)."),
        ("parse_preview", "`fec.csv` : Experio n'a pas pu lire le fichier (boom)."),
        ("execute_import", "FEC `fec.csv` refuse par Experio : boom"),
    ],
)
def test_experio_failing_a_call_of_the_import_is_a_stop_with_its_reason(importing, failing, said):
    importing[failing] = RuntimeError("boom")
    file_id = sent("fec.csv", FEC)
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "stop" and answer["message"] == said and kind_of(file_id) == "entries"


@pytest.mark.communication
def test_experio_refusing_the_import_after_its_test_gives_its_reason(importing, monkeypatch):
    real = setup.odoo_json

    def failing_import(model, method, *args, context=None):
        if method == "execute_import" and not args[-1]:
            raise RuntimeError("verrou")
        return real(model, method, *args, context=context)

    monkeypatch.setattr(setup, "odoo_json", failing_import)
    answer = setup.import_entries(557, sent("fec.csv", FEC))
    assert answer["status"] == "stop" and answer["message"] == "FEC `fec.csv` : Experio a refuse l'import (verrou)."


@pytest.mark.communication
def test_a_file_experio_cannot_read_is_a_stop_with_its_reason(importing):
    importing["preview"] = {"error": "separateur introuvable"}
    answer = setup.import_entries(557, sent("fec.csv", FEC))
    assert answer["message"] == "`fec.csv` : Experio n'a pas pu lire le fichier (separateur introuvable)."
    assert imports(importing) == []


@pytest.mark.security
def test_in_test_mode_experio_only_tests_the_file(importing, monkeypatch):
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    file_id = sent("fec.csv", FEC)
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "ok" and answer["message"] == (
        f"{TEST_MODE}FEC `fec.csv` : test Experio reussi (2 lignes), rien n'est importe."
    )
    assert [dry for dry, *_ in imports(importing)] == [True] and setup.waiting_setups("s")["dossiers"] == []


@pytest.mark.communication
def test_a_file_nothing_tells_is_asked_and_kept(importing):
    file_id = sent("liste.csv", "Colonne A;Colonne B\n1;2")
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "question" and "qu'est-ce que ce fichier" in answer["message"]
    assert importing["calls"] == [] and kind_of(file_id) is None  # the agent calls again with the client's answer


@pytest.mark.communication
@pytest.mark.parametrize(
    "name, mime, data, said",
    [
        ("scan.pdf", "application/pdf", b"%PDF-1.4", "envoyez le FEC ou le grand livre en CSV, TXT ou Excel"),
        ("fec.xlsx", "application/vnd.ms-excel", b"pas un classeur", "fichier Excel illisible"),
    ],
)
def test_a_file_that_is_no_table_is_a_stop_before_experio(importing, name, mime, data, said):
    file_id = sent(name, data, mime)
    answer = setup.import_entries(557, file_id)
    assert answer["status"] == "stop" and said in answer["message"] and importing["calls"] == []
    assert kind_of(file_id) == "entries"


@pytest.mark.functionality
def test_a_journals_file_names_the_banks_as_experio_lists_them(importing):
    importing["preview"] = {"headers": ["Code journal", "Banque"], "matches": {"0": ["code"]}}
    text = "Code journal;Banque\nBQ1;attijari\nBQ2;Chaabi\nCAI;"
    answer = setup.import_entries(557, sent("journaux.csv", text))
    upload = importing["calls"][0][2][0]
    assert upload["res_model"] == "exp.journal"
    assert upload["file"].splitlines() == ["Code journal;Banque", "BQ1;ATTIJARIWAFA BANK", "BQ2;", "CAI;"]
    assert imports(importing)[-1][1] == ["code", "bank_id"]
    assert answer["target"] == "journaux"
    assert answer["message"] == "Journaux `journaux.csv` importe dans le dossier `Gecafisc` : 3 lignes."  # no detection


@pytest.mark.functionality
def test_a_suppliers_file_opens_experios_suppliers_import(importing):
    importing["preview"] = {"headers": ["Nom", "IF"], "matches": {"0": ["name"]}}
    answer = setup.import_entries(557, sent("f.csv", "Nom;IF\nATLAS;123"), kind="fournisseurs")
    assert answer["target"] == "fournisseurs"
    model, _, args, context = importing["calls"][0]
    assert args[0]["res_model"] == "exp.partner" and context["default_is_supplier"] is True
    assert imports(importing)[-1][1] == ["name", "vat"]


@pytest.mark.functionality
def test_a_journals_file_without_a_bank_column_is_left_as_written(monkeypatch):
    monkeypatch.setattr(setup, "odoo_call", lambda *a, **k: pytest.fail("banks read"))
    assert setup.with_bank_names("Code;Nom\nBQ;Banque", ";") == "Code;Nom\nBQ;Banque"
    assert setup.with_bank_names("", ";") == ""


@pytest.mark.functionality
def test_a_column_matched_by_experio_keeps_its_field_and_a_field_is_never_taken_twice():
    preview = {"headers": ["Compte", "Code journal", "Nom du journal", "Compte"], "matches": {"1": ["code"]}}
    assert setup.columns(preview, "journaux") == ["default_account_id", "code", "name", False]
