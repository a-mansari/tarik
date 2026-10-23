"""The decisions the agent makes from the client's reply, measured on the real model: what it reads as oui or
non, which proposed tiers the client chose, a clear yes to a confirmation, the invoices' filter, the exercice and
the bank account chosen, and which waiting question the reply answers. Each kind must
be right on average (MIN_RIGHT) and almost never decide what the client did not (MAX_WRONG; never for a
confirmation): an unclear reply must leave the decision empty, so the client is asked again.

Slow and billed: python -m pytest -m llm -s (-s shows the cases missed and the scores)."""

import pytest

from tarik.core.conversation import chosen_among, yes_no
from tarik.core.words import normalized
from tarik.invoices.exercice import exercice_choice
from tarik.invoices.listing import STATUSES
from tarik.invoices.pipeline import meant

MIN_RIGHT = 0.9
MAX_WRONG = 0.05


def after(first, tool, given, result, said):
    """The conversation: the client's ``first`` message, the agent's call of ``tool`` with ``given`` and its
    ``result``, relayed to the client as is, then the client's reply ``said``."""
    return [
        {"role": "user", "content": first},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": tool, "input": given}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": result}]},
        {"role": "assistant", "content": result},
        {"role": "user", "content": said},
    ]


def processing(question, said, expected):
    """A case: the invoices processed, ``question`` asked, the client says ``said``."""
    given = {"dossier": "Gecafisc", "invoice_type": "achat"}
    messages = after("Voici les factures d'achat de GECAFISC.", "Experio_traiter_fichiers", given, question, said)
    return {"skill": "Réponse à une question", "messages": messages, "expected": expected, "said": said}


TVA = "- facture 12 : QUESTION : Facture de NOVA TELECOM : la TVA est-elle non recuperable (voyage, carburant, vehicule de tourisme) ? oui / non"  # noqa: E501
PREPAID = "- facture 14 : QUESTION : Facture F-2026-031 : elle couvre 12 mois (2026-01-01 au 2026-12-31). La repartir en charges constates d'avance ? oui / non"  # noqa: E501
OUI_NON = [
    processing(TVA, "Oui", "oui"),
    processing(TVA, "oui, elle n'est pas récupérable", "oui"),
    processing(TVA, "Elle est non récupérable", "oui"),
    processing(TVA, "Nous sommes d'accord, c'est du carburant, pas récupérable", "oui"),
    processing(TVA, "Yes", "oui"),
    processing(TVA, "Non", "non"),
    processing(TVA, "Non, elle est récupérable", "non"),
    processing(TVA, "récupérable", "non"),
    processing(TVA, "Ok non merci", "non"),
    processing(TVA, "Je ne sais pas, je vérifie avec le client", ""),
    processing(TVA, "oui... enfin non, attendez", ""),
    processing(TVA, "Si c'est un véhicule de tourisme, oui", ""),
    processing(TVA, "Pourquoi cette question ?", ""),
    processing(PREPAID, "Oui, répartissez-la", "oui"),
    processing(PREPAID, "vas-y", "oui"),
    processing(PREPAID, "Non, c'est une charge ponctuelle", "non"),
    processing(PREPAID, "Pas besoin", "non"),
    processing(PREPAID, "À voir avec mon comptable", ""),
]


def oui_non(call, expected):
    """The server's reading of the agent's decision (yes_no), against the client's."""
    if not call:  # the agent asked the client itself: nothing applied
        return "right" if expected == "" else "asked again"
    if call["name"] != "Experio_repondre":
        return "wrong"
    read = {True: "oui", False: "non", None: ""}[yes_no(str(call["input"].get("decision", "")))]
    return "right" if read == expected else "asked again" if not read else "wrong"


@pytest.mark.llm
def test_oui_or_non_is_read_as_the_client_meant_it(measure):
    right, wrong, _ = measure(OUI_NON, oui_non)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


TIERS_QUESTION = (
    "- facture 12 : QUESTION : Facture 12 : fournisseur « ATLAS » introuvable par ICE/IF. Tiers proches : "
    "ATLAS DE ROULEMENT (id 7, ICE 001234567000089) 92 %; ATLAS TRANSPORT (id 8) 88 %; ATLAS MAROC SARL (id 15) "
    "81 %. Lequel est-ce ? Sinon, donnez un autre nom ou l'ICE du tiers."
)
ICES = {"001234567000089": 7}
TIERS_CHOICES = {7: "ATLAS DE ROULEMENT", 8: "ATLAS TRANSPORT", 15: "ATLAS MAROC SARL"}
TIERS = [
    processing(TIERS_QUESTION, "Le premier", 7),
    processing(TIERS_QUESTION, "ATLAS TRANSPORT", 8),
    processing(TIERS_QUESTION, "c'est le troisième", 15),
    processing(TIERS_QUESTION, "Atlas Maroc", 15),
    processing(TIERS_QUESTION, "celui avec l'ICE 001234567000089", 7),
    processing(TIERS_QUESTION, "Aucun, c'est NOVA TELECOM", "nova telecom"),
    processing(TIERS_QUESTION, "Aucun des trois", None),
    processing(TIERS_QUESTION, "Je ne sais pas", None),
    processing(TIERS_QUESTION, "oui", None),
    processing(TIERS_QUESTION, "Atlas", None),
]


def tiers(call, expected):
    """The tiers the server links from the agent's decision (an id proposed, or an ICE), else the name it searches."""
    if not call:  # the agent asked the client itself: nothing linked
        return "right" if expected is None else "asked again"
    if call["name"] != "Experio_repondre":
        return "wrong"
    decision = str(call["input"].get("decision", "")).strip()
    linked = chosen_among(decision, TIERS_CHOICES) or ICES.get(decision)
    if linked:
        return "right" if linked == expected else "wrong"
    if isinstance(expected, str):  # another tiers named: searched
        return "right" if expected in normalized(decision) else "asked again"
    return "right" if expected is None else "asked again"


@pytest.mark.llm
def test_the_tiers_linked_is_the_one_the_client_chose(measure):
    right, wrong, _ = measure(TIERS, tiers)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


SKIP_ASKED = (
    "QUESTION : Experio verifie l'ICE et l'IF aupres du registre officiel. Confirmez-vous la creation du dossier "
    "ALPHA SARL sans cette verification ? oui / non"
)
CREATE = {
    "dossier_id": 0,
    "name": "ALPHA SARL",
    "ice": "001234567000089",
    "if_number": "",
    "folder_type": "company",
    "skip_ice_if_check": True,
    "client_confirmed": False,
    "model_dossier_id": 0,
    "kind": "",
}


def confirmation(said, expected):
    """A case: a dossier asked to be created without the registry check, its confirmation asked, the reply."""
    first = "Créez le dossier ALPHA SARL, ICE 001234567000089, sans la vérification du registre."
    messages = after(first, "Experio_parametrer_dossier", CREATE, SKIP_ASKED, said)
    return {"skill": "Nouveau dossier", "messages": messages, "expected": expected, "said": said}


CONFIRMATIONS = [
    confirmation("Oui, je confirme", True),
    confirmation("oui", True),
    confirmation("Ok, allez-y", True),
    confirmation("Non, attendez, je vérifie l'ICE", False),
    confirmation("Pourquoi ? C'est risqué ?", False),
    confirmation("Oui mais pour BETA, pas pour ALPHA", False),
    confirmation("Je ne sais pas", False),
    confirmation("Ok non merci", False),
]


def confirmed(call, expected):
    """Whether the agent calls back with client_confirmed true for the dossier asked about (the server takes it for
    that dossier only): never unless the client clearly said yes."""
    said_yes = bool(call) and call["name"] == "Experio_parametrer_dossier"
    said_yes = said_yes and str(call["input"].get("client_confirmed")).lower() == "true"
    said_yes = said_yes and normalized(str(call["input"].get("name"))) in normalized(SKIP_ASKED)
    if said_yes:
        return "right" if expected else "wrong"
    return "asked again" if expected else "right"


@pytest.mark.llm
def test_a_confirmation_is_only_a_clear_yes(measure):
    right, wrong, _ = measure(CONFIRMATIONS, confirmed)
    assert right >= MIN_RIGHT and wrong == 0  # a skipped check the client didn't confirm: never


FOUND = "GECAFISC (id 557) : 100 %"


def listing(said, expected):
    """A case: the client asks for a dossier's invoices (``said``), the dossier already found."""
    messages = [
        {"role": "user", "content": said},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "Experio_find_dossier", "input": {"name": "Gecafisc"}}
            ],
        },  # noqa: E501
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": FOUND}]},
    ]
    return {"skill": "Traitement des documents", "messages": messages, "expected": expected, "said": said}


LISTS = [
    listing("Montre-moi les factures impayées de Gecafisc", "comptabilisee"),
    listing("Quelles factures de Gecafisc sont payées ?", "payee"),
    listing("Les brouillons de Gecafisc", "brouillon"),
    listing("Les factures de Gecafisc qu'Experio est encore en train de lire", "en cours"),
    listing("Toutes les factures de Gecafisc", ""),
    listing("Les factures comptabilisées de Gecafisc", "comptabilisee"),
    listing("Liste les factures de Gecafisc", ""),
    listing("Gecafisc : quelles factures restent à payer ?", "comptabilisee"),
    listing("Combien de factures d'achat sont exportées chez Gecafisc ?", ("exportee", "achat")),
    listing("Les factures de vente payées de Gecafisc", ("payee", "vente")),
    listing("Les factures exportées de Gecafisc", "exportee"),
]


def status(call, expected):
    """The status (and side: achat, vente) the agent filters by: one the server accepts, the one the client asked
    for."""
    if not call or call["name"] != "Experio_list_invoices":
        return "asked again"
    expected, side = expected if isinstance(expected, tuple) else (expected, "")
    given = str(call["input"].get("status") or "").strip()
    if given == expected and str(call["input"].get("side") or "").strip() == side:
        return "right"
    return "wrong" if given in STATUSES or not given else "asked again"  # any other value is refused


@pytest.mark.llm
def test_the_invoices_shown_are_those_the_client_asked_for(measure):
    right, wrong, _ = measure(LISTS, status)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


OPENS = [
    {"id": 31, "name": "Exercice 2024", "date_start": "2024-01-01", "date_end": "2024-12-31"},
    {"id": 32, "name": "Exercice 2025", "date_start": "2025-01-01", "date_end": "2025-12-31"},
]
EXERCICE_QUESTION = (
    "- facture 12 : QUESTION : Facture 12 : Elle est datee du 2023-11-20, en dehors des exercices ouverts "
    "(Exercice 2024, Exercice 2025). Je la comptabilise dans un de ceux-la (comme facture anterieure, precisez "
    "lequel), ou je cree un nouvel exercice couvrant sa vraie date (repondez « nouveau ») ?"
)
EXERCICES = [
    processing(EXERCICE_QUESTION, "Mettez-la dans 2025", "Exercice 2025"),
    processing(EXERCICE_QUESTION, "Comptabilisez-la comme facture antérieure dans l'exercice 2024", "Exercice 2024"),
    processing(EXERCICE_QUESTION, "Dans la plus récente des deux", "Exercice 2025"),
    processing(EXERCICE_QUESTION, "nouveau", "nouveau"),
    processing(EXERCICE_QUESTION, "Créez un nouvel exercice", "nouveau"),
    processing(EXERCICE_QUESTION, "Pas besoin de créer un exercice, mettez-la dans l'exercice 2024", "Exercice 2024"),
    processing(EXERCICE_QUESTION, "Ne créez rien pour l'instant, je vérifie", None),
    processing(EXERCICE_QUESTION, "Dans l'exercice ouvert", None),
    processing(EXERCICE_QUESTION, "Je ne sais pas", None),
]


def exercice(call, expected):
    """The exercice the server books the invoice in from the agent's decision (exercice_choice): an open one or new."""
    if not call:
        return "right" if expected is None else "asked again"
    if call["name"] != "Experio_repondre":
        return "wrong"
    choice = exercice_choice(str(call["input"].get("decision", "")), OPENS)
    read = None if not choice else "nouveau" if choice[0] == "new" else choice[1]["name"]
    return "right" if read == expected else "asked again" if read is None else "wrong"


@pytest.mark.llm
def test_the_exercice_is_the_one_the_client_chose(measure):
    right, wrong, _ = measure(EXERCICES, exercice)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


BANK_QUESTION = (
    "- releve bancaire : QUESTION : releve_mars.pdf : le dossier GECAFISC a plusieurs comptes bancaires, lequel ? "
    "BMCE - BMCE BANK (n° 41), BP - BANQUE POPULAIRE (n° 42), AWB - ATTIJARIWAFA BANK (n° 43)"
)
BANK_CHOICES = {41: "BMCE - BMCE BANK", 42: "BP - BANQUE POPULAIRE", 43: "AWB - ATTIJARIWAFA BANK"}
BANKS = [
    processing(BANK_QUESTION, "Le compte BMCE", 41),
    processing(BANK_QUESTION, "j'ai 7 relevés, c'est le compte BP", 42),
    processing(BANK_QUESTION, "AWB", 43),
    processing(BANK_QUESTION, "Attijari", 43),
    processing(BANK_QUESTION, "la Banque Populaire", 42),
    processing(BANK_QUESTION, "le deuxième", 42),
    processing(BANK_QUESTION, "Je ne sais pas", 0),
    processing(BANK_QUESTION, "celui de la CIH", 0),
]


def bank(call, expected):
    """The bank journal the server imports the statement into from the agent's decision (chosen_among)."""
    if not call:
        return "right" if not expected else "asked again"
    if call["name"] != "Experio_repondre":
        return "wrong"
    chosen = chosen_among(str(call["input"].get("decision", "")), BANK_CHOICES) or 0
    return "right" if chosen == expected else "asked again" if not chosen else "wrong"


@pytest.mark.llm
def test_the_statement_goes_to_the_bank_account_the_client_chose(measure):
    right, wrong, _ = measure(BANKS, bank)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


TWO_QUESTIONS = (
    "- facture 11 : QUESTION : Facture FA-11 : quel compte utiliser ? (compte de charge, ex. 6125)\n"
    "- facture 12 : QUESTION : Facture de NOVA TELECOM : la TVA est-elle non recuperable (voyage, carburant, "
    "vehicule de tourisme) ? oui / non"
)
WAITING = [
    {"invoice_id": 11, "names": {"11"}, "step": "assign_account"},
    {"invoice_id": 12, "names": {"12"}, "step": "settle_tva"},
]
ABOUT = [
    processing(TWO_QUESTIONS, "Oui pour Nova Telecom", 12),
    processing(TWO_QUESTIONS, "Pour la 11 : compte 6125", 11),
    processing(TWO_QUESTIONS, "La facture FA-11 va en 6125", 11),
    processing(TWO_QUESTIONS, "Compte 6125", 11),
    processing(TWO_QUESTIONS, "Non, elle est récupérable", 12),
    processing(TWO_QUESTIONS, "Pour laquelle ? Je ne comprends pas", None),
]


def about(call, expected):
    """The question the server applies the reply to from the agent's ``about`` (meant), among those waiting."""
    if not call:
        return "right" if expected is None else "asked again"
    if call["name"] != "Experio_repondre":
        return "wrong"
    item = meant(WAITING, str(call["input"].get("about", "")))
    read = item and item["invoice_id"]
    return "right" if read == expected else "asked again" if not read else "wrong"


@pytest.mark.llm
def test_a_reply_goes_to_the_question_it_answers(measure):
    right, wrong, _ = measure(ABOUT, about)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG
