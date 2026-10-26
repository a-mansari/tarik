"""The first move Tarik makes on a client's request, the real model shown the agent as it starts (no skill loaded):
the skill that does it, or the read tool that answers it; and, the skill loaded, the dossier's name kept whole. The
situations are clients' own words, those that confused Tarik first (2026-10-21: « creer un dossier HTML TEST »
searched for « TEST », then read « dossier HTML » as a web folder and refused)."""

import pytest

from engine.tools.core.words import normalized

MIN_RIGHT, MAX_WRONG = 0.9, 0.05


def asked(said, before=()):
    """A case: the client's message ``said``, after the turns ``before`` ((role, text) pairs) when given."""
    earlier = [{"role": role, "content": text} for role, text in before]
    return {"said": said, "messages": [*earlier, {"role": "user", "content": said}]}


GREETED = (("user", "hi"), ("assistant", "Bonjour, comment puis-je vous aider ?"))


# What the client writes first, and the first move expected: a skill to load, or a tool to call.
FIRST_MOVES = [
    {"skill": None, **asked("creer un dossier HTML TEST"), "expected": "Nouveau dossier"},
    {"skill": None, **asked("creer un dossier HTML TEST", GREETED), "expected": "Nouveau dossier"},  # as it happened
    {"skill": None, **asked("Créez un nouveau dossier pour ALPHA DIGITAL SARL"), "expected": "Nouveau dossier"},
    {"skill": None, **asked("nouveau client : Madame Salma Bennani"), "expected": "Nouveau dossier"},
    {"skill": None, **asked("Voici les factures d'achat de GECAFISC"), "expected": "Traitement des documents"},
    {"skill": None, **asked("Changez l'ICE de TARIK TEST : 001122334455667"), "expected": "Modification de dossier"},
    {"skill": None, **asked("Archivez le dossier TEST SARL"), "expected": "Gestion des dossiers"},
    {"skill": None, **asked("Experio fonctionne ?"), "expected": "Experio_experio_connection"},
    # 2026-10-26: « dossier d'import » read as a dossier to create, its pieces left aside
    {
        "skill": None,
        **asked("dossier d'import TARIK TEST\n\nFichiers joints : 17-dossier-import.pdf"),
        "expected": "Traitement des documents",
    },
    {
        "skill": None,
        **asked("gasoil TARIK TEST\n\nFichiers joints : 03-carburant.pdf"),
        "expected": "Traitement des documents",
    },
]


# A skill's own first step, as good as loading it: what its instructions start with. A new dossier's has none but
# its creation: a search first is where Tarik lost the name (« HTML TEST » searched as « TEST »).
STARTS = {
    "Nouveau dossier": {"Experio_parametrer_dossier"},
    "Traitement des documents": {"Experio_traiter_fichiers"},
    "Modification de dossier": {"Experio_find_dossier"},
    "Gestion des dossiers": {"Experio_find_dossier", "Experio_importer_dossiers"},
}


def first_move(call, expected):
    if not call:
        return "asked again"  # a question to the client: never a wrong move, but not the one asked for
    if call["name"] == "load_skill":
        return "right" if call["input"].get("name") == expected else "wrong"
    return "right" if call["name"] == expected or call["name"] in STARTS.get(expected, ()) else "wrong"


@pytest.mark.llm
def test_a_request_goes_first_to_what_does_it(measure):
    right, wrong, _ = measure(FIRST_MOVES, first_move)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG


# The skill loaded: the new dossier's name as the client wrote it, nothing cut off, nothing read into it.
NAMES = [
    {"skill": "Nouveau dossier", **asked("creer un dossier HTML TEST"), "expected": ("HTML TEST", "company")},
    {"skill": "Nouveau dossier", **asked("creer un dossier HTML TEST", GREETED), "expected": ("HTML TEST", "company")},
    {"skill": "Nouveau dossier", **asked("Créez le dossier SARL WEB & CO"), "expected": ("SARL WEB & CO", "company")},
    {
        "skill": "Nouveau dossier",
        **asked("nouveau client : Madame Salma Bennani"),
        "expected": ("Salma Bennani", "person"),
    },
]


def created(call, expected):
    if not call:
        return "asked again"
    if call["name"] != "Experio_parametrer_dossier" or call["input"].get("dossier_id") not in (0, "0"):
        return "wrong"
    name, kind = expected
    same = normalized(str(call["input"].get("name", ""))) == normalized(name)
    return "right" if same and call["input"].get("folder_type") == kind else "wrong"


@pytest.mark.llm
def test_a_new_dossier_keeps_the_name_the_client_wrote(measure):
    right, wrong, _ = measure(NAMES, created)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG
