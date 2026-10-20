"""A list of dossiers, a collaborateur or client, an archiving: checked, read back, written only on the client's yes."""

import pytest

from tarik.core.app import ToolError
from tarik.dossiers import management
from tarik.dossiers.management import problems_of, read_rows

HEAD = "Nom;Type;ICE;IF;Mode de declaration;Ville;RAS TVA"


def rows_of(text):
    return read_rows("dossiers.csv", "text/csv", text.encode())


@pytest.fixture
def experio(monkeypatch):
    """Experio with one dossier, ALPHA (ICE 001...089); what was created."""
    created = []
    monkeypatch.setattr(management, "dossiers", lambda: [{"id": 1, "name": "ALPHA SARL"}])
    monkeypatch.setattr(
        management,
        "having",
        lambda keys, *extra: [{"name": "ALPHA SARL"}] if ("ice", "001234567000089") in keys else [],
    )
    monkeypatch.setattr(
        management, "country_of", lambda term: {"id": 5, "name": "Maroc"} if term.lower() in ("ma", "maroc") else None
    )
    monkeypatch.setattr(management, "dry_run", lambda: False)
    monkeypatch.setattr(management, "attach_user", lambda user_id, dossier_id, *a: None)
    monkeypatch.setattr(management, "odoo_json", lambda model, method, values: created.append(values) or 9)
    return created


@pytest.mark.functionality
def test_the_rows_as_written_and_what_is_wrong_with_them(experio):
    text = (
        f"{HEAD}\nBETA;societe;1234567000089;12345678;mensuelle;Rabat;oui\n"
        "ALPHA SARL;personne physique;;;trimestrielle;;\n"
        "GAMMA;SA;001234567000089;abc;annuelle;Rabat;peut-etre\n;;;;;;\nBETA;societe;;;mensuelle;;"
    )
    rows = rows_of(text)
    assert rows[0]["ice"] == "001234567000089" and rows[0]["ras_tva"] is True  # leading zeros back, oui read
    assert [p.split(" : ", 1)[1] for p in problems_of(rows)] == [
        "l'ICE ou l'IF est deja celui du dossier ALPHA SARL",
        "le dossier ALPHA SARL existe deja",
        "type « SA » inconnu (societe, personne physique, CPU, ST)",
        "IF « abc » incorrect (chiffres uniquement)",
        "mode de declaration « annuelle » inconnu (mensuelle, trimestrielle, hors champ)",
        "RAS TVA « peut-etre » incompris (oui ou non)",
        "meme ICE que la ligne 2",
        "l'ICE ou l'IF est deja celui du dossier ALPHA SARL",
        "en double avec la ligne 2",
    ]
    with pytest.raises(ToolError, match="il manque des colonnes.*ICE"):
        rows_of("Nom;Type\nBETA;societe")


@pytest.mark.security
def test_the_dossiers_are_created_only_on_the_clients_yes_to_the_list(experio, monkeypatch):
    text = f"{HEAD}\nBETA;societe;;;mensuelle;Rabat;\nDELTA;personne physique;;;hors champ;;"
    monkeypatch.setattr(management, "cabinet_db", lambda: pytest.fail("no file"))
    monkeypatch.setattr(management, "read_rows", lambda *a: rows_of(text))
    monkeypatch.setattr(management.cabinet_db.__class__, "__call__", lambda *a: None) if False else None

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=()):
            return self

        def fetchone(self):
            return ("dossiers.csv", "text/csv", b"")

    monkeypatch.setattr(management, "cabinet_db", lambda: Db())
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    asked = management.import_dossiers(7, False, "s")
    assert (
        asked["status"] == "question" and "- BETA : societe" in asked["message"] and "Je les cree ?" in asked["message"]
    )
    assert experio == []
    done = management.import_dossiers(7, True, "s")
    assert done["message"] == "dossiers.csv : 2 dossier(s) cree(s) sur 2."
    assert experio[0] == {
        "name": "BETA",
        "folder_type": "company",
        "declaration_mode": "monthly",
        "enabled": True,
        "city": "Rabat",
    }


@pytest.mark.security
def test_a_person_is_made_of_the_clients_words_and_their_yes(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id: "BETA")
    monkeypatch.setattr(management, "client_messages", lambda session_id, message: [message])
    monkeypatch.setattr(management, "profiles", lambda kind: {"Collaborateur": 1, "Chef de mission": 2})
    monkeypatch.setattr(management, "user_by_email", lambda email, kind: None)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    written = []
    monkeypatch.setattr(management, "odoo_json", lambda model, method, values: written.append(values) or 42)
    monkeypatch.setattr(management, "attach_user", lambda *args: written.append(args))
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    message = "Ajoute Samira Alami (samira@cabinet.ma) comme chef de mission sur BETA"
    missing = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "", False, message, "s"
    )
    assert missing["status"] == "question" and "son profil (Collaborateur, Chef de mission)" in missing["message"]
    invented = management.create_person(
        "collaborator", 5, "Samira Alami", "s.alami@cabinet.ma", "", "chef de mission", False, message, "s"
    )
    assert "son e-mail" in invented["message"] and "email : pas ecrit par le client" in invented["message"]
    asked = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "chef de mission", False, message, "s"
    )
    assert asked["status"] == "question" and "Vous confirmez ?" in asked["message"] and written == []
    done = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "chef de mission", True, message, "s"
    )
    assert done["message"].startswith("Collaborateur Samira Alami (samira@cabinet.ma) cree et rattache au dossier BETA")
    assert (
        written[0]["login"] == "samira@cabinet.ma"
        and written[0]["exp_profile_id"] == 2
        and written[1] == (42, 5, 2, "employee")
    )


@pytest.mark.security
def test_archiving_reads_back_and_waits_for_the_yes(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id: "BETA")
    monkeypatch.setattr(management, "odoo_call", lambda model, method, domain: 12 if len(domain) == 1 else 7)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    archived = []
    monkeypatch.setattr(management, "odoo_json", lambda model, method, ids: archived.append(ids))
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    asked = management.archive_dossier(5, False, "s")
    assert "BETA (12 facture(s), dont 7 comptabilisee(s))" in asked["message"] and archived == []
    assert management.archive_dossier(5, True, "s")["message"].startswith("Dossier BETA archive.") and archived == [[5]]
