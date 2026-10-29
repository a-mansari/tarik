"""A list of dossiers, a collaborateur or client, an archiving: checked, read back, written only on the client's yes."""

import re

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.dossiers import management
from engine.tools.dossiers.management import problems_of, read_rows
from tests.fakes import FakeDb

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
        "l'ICE ou l'IF est déjà celui du dossier `ALPHA SARL`",
        "le dossier `ALPHA SARL` existe déjà",
        "type « SA » inconnu (société, personne physique, CPU, ST)",
        "IF « abc » incorrect (chiffres uniquement)",
        "mode de déclaration « annuelle » inconnu (mensuelle, trimestrielle, hors champ)",
        "RAS TVA « peut-etre » incompris (oui ou non)",
        "même ICE que la ligne 2",
        "l'ICE ou l'IF est déjà celui du dossier `ALPHA SARL`",
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
            return ("s", "dossiers.csv", "text/csv", b"", None, None, None, None)  # Q.file_locked's columns

    monkeypatch.setattr(management, "cabinet_db", lambda: Db())
    monkeypatch.setattr(management, "taken", lambda file_id: None)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    asked = management.import_dossiers(7, False, "s")
    assert (
        asked["status"] == "question"
        and "- `BETA` : société" in asked["message"]
        and "Je les crée ?" in asked["message"]
    )
    assert experio == []
    done = management.import_dossiers(7, True, "s")
    assert done["message"] == "`dossiers.csv` : 2 dossiers créés sur 2."
    assert experio[0] == {
        "name": "BETA",
        "folder_type": "company",
        "declaration_mode": "monthly",
        "enabled": True,
        "city": "Rabat",
    }


@pytest.mark.security
def test_a_person_is_made_of_the_clients_words_and_their_yes(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "client_messages", lambda session_id, message: [message])
    monkeypatch.setattr(management, "profiles", lambda kind: {"Collaborateur": 1, "Chef de mission": 2})
    monkeypatch.setattr(management, "user_by_email", lambda email, kind: None)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    written = []
    monkeypatch.setattr(management, "odoo_json", experio_users(written))
    monkeypatch.setattr(management, "attach_user", lambda *args: written.append(args))
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    message = "Ajoute Samira Alami (samira@cabinet.ma) comme chef de mission sur BETA"
    missing = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "", False, message, "s"
    )
    assert missing["status"] == "question" and "son profil (`Collaborateur`, `Chef de mission`)" in missing["message"]
    invented = management.create_person(
        "collaborator", 5, "Samira Alami", "s.alami@cabinet.ma", "", "chef de mission", False, message, "s"
    )
    assert "son e-mail" in invented["message"] and "email : pas écrit par le client" in invented["message"]
    asked = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "chef de mission", False, message, "s"
    )
    assert asked["status"] == "question" and "Vous confirmez ?" in asked["message"] and written == []
    done = management.create_person(
        "collaborator", 5, "Samira Alami", "samira@cabinet.ma", "", "chef de mission", True, message, "s"
    )
    assert done["message"].startswith(
        "Collaborateur `Samira Alami` (`samira@cabinet.ma`) créé et rattaché au dossier `BETA`"
    )
    assert done["message"].endswith(
        " J'ai envoyé l'invitation Experio à `samira@cabinet.ma`, avec son identifiant et son mot de passe."
    )
    assert written[0] == {
        "name": "Samira Alami",
        "login": "samira@cabinet.ma",  # no "email": Odoo's sign-up mail is not Experio's invitation
        "employee_type": "employee",
        "exp_profile_id": 2,
        "context": {"no_reset_password": True},
    }
    assert written[1] == (42, 5, 2, "employee") and written[2] == ("action_invite_user", [42])


def experio_users(written):
    """odoo_json for res.users: a create (its values and context kept) gives id 42; a button call is kept as (method,
    ids)."""

    def odoo_json(model, method, *args, **kw):
        if method == "create":
            written.append({**args[0], **kw})
            return 42
        written.append((method, *args))
        return True

    return odoo_json


@pytest.mark.security
def test_archiving_reads_back_and_waits_for_the_yes(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "odoo_call", lambda model, method, domain: 12 if len(domain) == 1 else 7)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    archived = []
    monkeypatch.setattr(management, "odoo_json", lambda model, method, ids: archived.append(ids))
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    asked = management.archive_dossier(5, False, "s")
    assert "`BETA`, qui a 12 factures, dont 7 comptabilisées." in asked["message"] and archived == []
    done = management.archive_dossier(5, True, "s")
    assert done["message"].startswith("Dossier `BETA` archivé.") and archived == [[5]]


@pytest.mark.communication
@pytest.mark.parametrize(
    "name, data, said",
    [
        ("dossiers.pdf", b"%PDF", "envoyez la liste des dossiers en Excel (.xlsx) ou en CSV"),
        ("dossiers.xlsx", b"pas un classeur", "fichier illisible"),
        ("dossiers.csv", b";;\n", "le fichier est vide"),
        ("dossiers.csv", HEAD.encode(), "aucune ligne de dossier sous l'en-tête"),
    ],
)
def test_a_list_that_cannot_be_read_says_why(name, data, said):
    with pytest.raises(ToolError, match=re.escape(said)):
        read_rows(name, "application/octet-stream", data)


@pytest.mark.functionality
def test_a_country_is_found_by_its_code_or_name_in_french_or_english(monkeypatch):
    asked = []

    def odoo_call(model, method, domain, fields, limit, context):
        asked.append((domain[0][0], context["lang"]))
        return [{"id": 5, "name": "Maroc"}] if (domain[0][0], context["lang"]) == ("name", "en_US") else []

    monkeypatch.setattr(management, "odoo_call", odoo_call)
    assert management.country_of("Morocco") == {"id": 5, "name": "Maroc"}
    assert asked == [("code", "fr_FR"), ("code", "en_US"), ("name", "fr_FR"), ("name", "en_US")]
    monkeypatch.setattr(management, "odoo_call", lambda *a, **k: [])
    assert management.country_of("Atlantide") is None


@pytest.mark.functionality
def test_a_rows_country_ice_and_name_are_checked(experio):
    rows = rows_of(f"{HEAD};Pays\nBETA;societe;12AB;;mensuelle;;;Atlantide\n;societe;;;mensuelle;;;\n")
    assert [p.split(" : ", 1)[1] for p in problems_of(rows)] == [
        "ICE « 12AB » incorrect (15 chiffres)",
        "pays « Atlantide » inconnu",
        "le nom est vide",
    ]


@pytest.fixture
def listing(experio, monkeypatch):
    """The client's spreadsheet (``listing["text"]``) sent as file 7, read back with their yes."""
    state = {"text": f"{HEAD};Pays;Code postal\nBETA;societe;;;mensuelle;Casablanca;non;MA;20000"}
    monkeypatch.setattr(management, "cabinet_db", lambda: FakeDb(("s", "dossiers.csv", "text/csv", b"", *[None] * 4)))
    monkeypatch.setattr(management, "read_rows", lambda *a: rows_of(state["text"]))
    monkeypatch.setattr(management, "taken", lambda file_id: None)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    return state


@pytest.mark.functionality
def test_a_rows_country_town_and_ras_tva_are_read_back_and_created(listing, experio):
    asked = management.import_dossiers(7, False, "s")
    assert (
        "- `BETA` : société, ICE non renseigné, IF non renseigné, mensuelle, 20000 Casablanca, Maroc, RAS TVA non"
        in (asked["message"])
    )
    management.import_dossiers(7, True, "s")
    assert experio[0] == {
        "name": "BETA",
        "folder_type": "company",
        "declaration_mode": "monthly",
        "enabled": True,
        "city": "Casablanca",
        "zip": "20000",
        "country_id": 5,
        "declaration_ras_tva": False,
    }


@pytest.mark.communication
def test_a_list_with_problems_creates_nothing_and_shows_the_first_ones(listing, experio):
    listing["text"] = HEAD + "".join(f"\nD{n};SA;;;mensuelle;;" for n in range(17))
    answer = management.import_dossiers(7, True, "s")
    assert answer["status"] == "stop" and "le fichier a 17 problèmes" in answer["message"]
    assert "- ... et 2 autres" in answer["message"] and experio == []


@pytest.mark.communication
def test_a_long_list_is_read_back_in_part(listing):
    listing["text"] = HEAD + "".join(f"\nD{n};societe;;;mensuelle;;" for n in range(17))
    asked = management.import_dossiers(7, False, "s")
    assert "17 dossiers, tous corrects" in asked["message"] and "- ... et 2 autres" in asked["message"]
    assert "- D15" not in asked["message"]


@pytest.mark.security
def test_in_test_mode_the_list_is_only_said(listing, experio, monkeypatch):
    monkeypatch.setattr(management, "dry_run", lambda: True)
    answer = management.import_dossiers(7, True, "s")
    assert answer["message"] == f"{TEST_MODE}`dossiers.csv` : 1 dossier serait créé." and experio == []


@pytest.mark.communication
def test_the_rows_experio_refuses_are_listed_and_the_others_created(listing, experio, monkeypatch):
    listing["text"] = f"{HEAD}\nBETA;societe;;;mensuelle;;\nDELTA;societe;;12345678;mensuelle;;"
    attached = []
    monkeypatch.setattr(management, "attach_user", lambda user_id, dossier_id: attached.append(dossier_id))

    def odoo_json(model, method, values):
        if values.get("vat"):
            raise RuntimeError("IF inconnu du registre")
        return 9

    monkeypatch.setattr(management, "odoo_json", odoo_json)
    answer = management.import_dossiers(7, True, "s")
    assert answer["message"] == (
        "`dossiers.csv` : 1 dossier créé sur 2.\nExperio a refusé ces lignes :\n"
        "- Ligne 3 (`DELTA`) : IF inconnu du registre"
        "\nCorrigez ces lignes et renvoyez-les seules."
    )
    assert attached == [9]


@pytest.mark.communication
def test_experio_not_checking_the_list_is_said(listing, monkeypatch):
    monkeypatch.setattr(management, "dossiers", lambda: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(ToolError, match="Experio n'a pas pu vérifier la liste des dossiers"):
        management.import_dossiers(7, True, "s")


@pytest.mark.functionality
def test_the_profiles_are_experios_own(monkeypatch):
    asked = []
    found = [{"id": 1, "name": "Collaborateur"}, {"id": 2, "name": "Chef de mission"}]
    monkeypatch.setattr(management, "odoo_call", lambda model, method, domain, **kw: asked.append(domain) or found)
    assert management.profiles("customer") == {"Collaborateur": 1, "Chef de mission": 2}
    assert asked == [[("type", "=", "customer")]]


@pytest.mark.functionality
def test_a_person_is_found_by_their_email_among_their_kind(monkeypatch):
    asked = []
    monkeypatch.setattr(
        management, "users_by_email", lambda email, types, fields, limit: asked.append(types) or [{"id": 4}][:limit]
    )
    assert management.user_by_email("a@b.ma", "client") == {"id": 4} and asked == [["client"]]
    monkeypatch.setattr(management, "users_by_email", lambda *a: [])
    assert management.user_by_email("a@b.ma", "client") is None


@pytest.fixture
def person(monkeypatch):
    """Dossier BETA, Experio's client profiles, and what was written."""
    written = []
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "client_messages", lambda session_id, message: [message])
    monkeypatch.setattr(management, "profiles", lambda kind: {"Client": 7})
    monkeypatch.setattr(management, "user_by_email", lambda email, kind: None)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    monkeypatch.setattr(management, "odoo_json", experio_users(written))
    monkeypatch.setattr(management, "attach_user", lambda *args: written.append(args))
    return written


MESSAGE = "Ajoute le client Karim Idrissi, karim@beta.ma, tel 0612345678, profil client"


@pytest.mark.functionality
def test_a_client_is_created_with_the_phone_the_client_wrote(person):
    asked = management.create_person(
        "client", 5, "Karim Idrissi", "karim@beta.ma", "0612345678", "client", False, MESSAGE, "s"
    )
    assert "- Téléphone : 0612345678\n" in asked["message"] and person == []
    done = management.create_person(
        "client", 5, "Karim Idrissi", "karim@beta.ma", "0612345678", "client", True, MESSAGE, "s"
    )
    assert done["message"].startswith(
        "Client `Karim Idrissi` (`karim@beta.ma`) créé et rattaché au dossier `BETA`, profil `Client`. "
        "J'ai envoyé l'invitation"
    )
    assert person[0]["phone"] == "0612345678" and person[0]["employee_type"] == "client"
    assert person[1] == (42, 5, 7, "client") and person[2] == ("action_invite_user", [42])


@pytest.mark.functionality
def test_a_person_already_in_experio_is_only_attached(person, monkeypatch):
    known = {"id": 8, "name": "K. Idrissi", "login": "karim@beta.ma", "employee_state": "active"}
    monkeypatch.setattr(management, "user_by_email", lambda email, kind: known)
    asked = management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", False, MESSAGE, "s")
    assert asked["message"].startswith("Le client `K. Idrissi` existe déjà (`karim@beta.ma`). Avant de le rattacher")
    done = management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    assert done["message"] == "Client `Karim Idrissi` (`karim@beta.ma`) rattaché au dossier `BETA`, profil `Client`."
    assert person == [(8, 5, 7, "client")]  # already invited: no new password sent
    known["employee_state"] = "new"  # made in Experio, never invited: invited now
    done = management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    assert person[-1] == ("action_invite_user", [8]) and "J'ai envoyé l'invitation Experio" in done["message"]


@pytest.mark.communication
def test_an_invitation_experio_refuses_leaves_the_person_made_and_says_why(person, monkeypatch):
    def refusing(model, method, *args, **kw):
        if method == "action_invite_user":
            raise RuntimeError("Traceback\nUserError: Maximum number of active employees is reached")
        return 42

    monkeypatch.setattr(management, "odoo_json", refusing)
    monkeypatch.setattr(management, "experio_refusal", lambda error: "nombre maximal d'utilisateurs atteint")
    done = management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    assert done["status"] == "ok" and "créé et rattaché" in done["message"]
    refusal = "Je n'ai pas pu envoyer l'invitation, car Experio refuse : nombre maximal d'utilisateurs atteint"
    assert refusal in done["message"]


@pytest.mark.communication
def test_a_name_the_client_did_not_write_is_asked(person):
    asked = management.create_person("client", 5, "Karim Alaoui", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    assert asked["message"] == (
        "Pour créer ce client sur le dossier `BETA`, il me faut son nom. (name : pas écrit par le client)"
    )
    assert person == []


@pytest.mark.functionality
def test_a_person_of_no_known_kind_is_refused(person):
    with pytest.raises(ToolError, match="collaborator ou client"):
        management.create_person("fournisseur", 5, "Karim Idrissi", "karim@beta.ma")


@pytest.mark.security
def test_in_test_mode_the_person_is_only_said(person, monkeypatch):
    monkeypatch.setattr(management, "dry_run", lambda: True)
    done = management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    assert done["message"] == f"{TEST_MODE}Client `Karim Idrissi` (`karim@beta.ma`) serait rattaché au dossier `BETA`."
    assert person == []


@pytest.mark.communication
def test_experio_failing_a_person_says_where(person, monkeypatch):
    monkeypatch.setattr(management, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("login deja pris")))
    with pytest.raises(ToolError, match="Experio refuse ce client : login deja pris"):
        management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    monkeypatch.setattr(management, "user_by_email", lambda *a: 1 / 0)
    with pytest.raises(ToolError, match="Experio n'a pas pu vérifier cet utilisateur"):
        management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")
    monkeypatch.setattr(management, "profiles", lambda kind: 1 / 0)
    with pytest.raises(ToolError, match="Experio n'a pas pu lire les profils"):
        management.create_person("client", 5, "Karim Idrissi", "karim@beta.ma", "", "client", True, MESSAGE, "s")


@pytest.mark.security
def test_in_test_mode_nothing_is_archived(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "odoo_call", lambda model, method, domain: 0)
    monkeypatch.setattr(management, "dry_run", lambda: True)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    monkeypatch.setattr(management, "odoo_json", lambda *a: pytest.fail("archived"))
    assert management.archive_dossier(5, True, "s")["message"] == f"{TEST_MODE}Le dossier `BETA` serait archivé."


@pytest.mark.communication
def test_experio_failing_the_archiving_says_where(monkeypatch):
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "odoo_call", lambda *a: 1 / 0)
    with pytest.raises(ToolError, match="`BETA` : Experio n'a pas pu compter ses factures"):
        management.archive_dossier(5, True, "s")
    monkeypatch.setattr(management, "odoo_call", lambda model, method, domain: 0)
    monkeypatch.setattr(management, "dry_run", lambda: False)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    monkeypatch.setattr(management, "odoo_json", lambda *a: (_ for _ in ()).throw(RuntimeError("factures ouvertes")))
    with pytest.raises(ToolError, match="Experio refuse d'archiver le dossier `BETA` : factures ouvertes"):
        management.archive_dossier(5, True, "s")


ACCESS_MESSAGE = "Donne le dossier BETA a samira@cabinet.ma, et passe-la chef de mission"


@pytest.fixture
def access(monkeypatch):
    """Samira, a collaborateur on dossier 1 only; what was written in Experio."""
    state = {"user": {"id": 42, "name": "Samira Alami", "login": "samira@cabinet.ma", "employee_type": "employee"}}
    state["user"].update(employee_state="active", company_ids=[1], exp_profile_id=[1, "Collaborateur"])
    state["written"] = []
    monkeypatch.setattr(management, "require_dossier", lambda dossier_id, **kw: "BETA")
    monkeypatch.setattr(management, "client_messages", lambda session_id, message: [message])
    monkeypatch.setattr(management, "profiles", lambda kind: {"Collaborateur": 1, "Chef de mission": 2})
    monkeypatch.setattr(
        management, "person_named", lambda email: state["user"] if email == "samira@cabinet.ma" else None
    )
    monkeypatch.setattr(management, "dry_run", lambda: False)
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: yes)
    monkeypatch.setattr(management, "sender_reach", lambda session_id: None)  # the chat: no sender to limit
    monkeypatch.setattr(management, "attach_user", lambda *args: state["written"].append(("attach", *args)))

    def odoo_call(model, method, *args, **kw):
        state["written"].append((model, method, *args))
        return [] if method == "search" else True

    monkeypatch.setattr(management, "odoo_call", odoo_call)
    monkeypatch.setattr(management, "odoo_json", odoo_call)
    return state


def changed(action, dossier_id=5, profile="", yes=True, message=ACCESS_MESSAGE, email="samira@cabinet.ma"):
    return management.change_access(action, email, dossier_id, profile, yes, message, "s")


@pytest.mark.security
def test_an_access_change_is_read_back_and_made_only_on_the_clients_yes(access):
    asked = changed("assign", yes=False)
    assert asked["status"] == "question" and "lui donner le dossier\n- Dossier : `BETA`" in asked["message"]
    assert access["written"] == []
    done = changed("assign")
    assert done["message"] == "Collaborateur `Samira Alami` (`samira@cabinet.ma`) : dossier donné (dossier `BETA`)."
    assert access["written"] == [("attach", 42, 5, None, "employee")]


@pytest.mark.security
def test_a_person_the_client_did_not_write_is_never_changed(access):
    asked = changed("unassign", 1, email="karim@beta.ma")
    assert asked["status"] == "question" and "Donnez son e-mail" in asked["message"] and access["written"] == []


@pytest.mark.functionality
def test_a_dossier_taken_back_and_nothing_to_change(access):
    assert changed("unassign", 5)["routine"]  # not on it: nothing to take
    assert changed("assign", 1)["routine"]  # already on it
    changed("unassign", 1)
    assert access["written"] == [("res.company", "write", [1], {"employee_ids": [(3, 42)]})]


@pytest.mark.functionality
def test_a_role_everywhere_or_on_one_dossier(access):
    asked = changed("role", 0)
    assert "Quel profil" in asked["message"] and "`Chef de mission`" in asked["message"]
    changed("role", 0, "chef de mission")
    assert access["written"] == [("res.users", "write", [42], {"exp_profile_id": 2})]
    access["written"].clear()
    done = changed("role", 1, "chef de mission")
    row = {"user_id": 42, "company_id": 1, "profile_id": 2}
    assert access["written"][-1] == ("exp.user.company.profile", "create", row)
    assert done["message"].endswith("profil changé sur ce dossier (dossier `BETA`), profil `Chef de mission`.")


@pytest.mark.functionality
def test_an_invitation_sent_again(access):
    changed("invite", 0)
    assert access["written"] == [("res.users", "action_invite_or_reinvite_user", [42])]


@pytest.mark.functionality
def test_an_unknown_access_action_is_refused(access):
    with pytest.raises(ToolError, match="assign, unassign, role ou invite"):
        changed("delete")


@pytest.fixture
def sender(access, monkeypatch):
    """Samira's request comes from Chef, an approver on dossier 5 only; Karim is an admin. Profile 2 is « user », 3
    « admin »."""
    levels = {1: "none", 2: "user", 3: "admin", 9: "admin"}
    monkeypatch.setattr(
        management, "level_of", lambda profile: levels[profile[0] if isinstance(profile, list) else profile]
    )
    reach = {"id": 7, "name": "Chef", "company_ids": [5], "rank": management.RANK["approver"]}
    monkeypatch.setattr(management, "sender_reach", lambda session_id: reach)
    monkeypatch.setattr(management, "profiles", lambda kind: {"Collaborateur": 1, "Chef de mission": 2, "Admin": 3})
    return reach


@pytest.mark.security
def test_a_sender_changes_access_only_on_their_dossiers_and_never_above_themselves(access, sender):
    refused = changed("assign", 1)  # dossier 1 is not Chef's
    assert refused["status"] == "stop" and "n'est pas attribué à `Chef`" in refused["message"]
    assert "plus de droits" in changed("role", 0, "admin")["message"]  # a profile above Chef's own
    access["user"]["exp_profile_id"] = [9, "Admin"]
    assert "`Samira Alami` a plus de droits que `Chef`" in changed("invite", 0)["message"]  # no reset of an admin
    access["user"].update(exp_profile_id=[1, "Collaborateur"], id=7)
    assert "son propre profil" in changed("role", 0, "chef de mission")["message"]
    access["user"]["id"] = 42
    assert "seuls les administrateurs" in changed("role", 5, "chef de mission")["message"]  # a dossier's profile
    assert access["written"] == []
    assert changed("assign", 5)["status"] == "ok"  # their own dossier, someone not above them


@pytest.mark.security
def test_a_yes_confirms_only_the_change_read_back(access, monkeypatch):
    asked = []
    monkeypatch.setattr(management, "confirmed", lambda session_id, yes, *parts: asked.append(parts) or yes)
    changed("unassign", 1)
    assert asked == [("vous confirmez", "samira@cabinet.ma", "lui retirer le dossier", "BETA", "")]


@pytest.mark.functionality
def test_a_dossier_given_with_a_profile_sets_it_even_when_already_on_it(access):
    done = changed("assign", 1, "chef de mission")
    row = {"user_id": 42, "company_id": 1, "profile_id": 2}
    assert access["written"][-1] == ("exp.user.company.profile", "create", row) and done["status"] == "ok"
    assert "n'a pas le dossier" in changed("role", 5, "chef de mission")["message"]  # given first, then its profile
