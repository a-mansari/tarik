"""The accounts the client gives invoices beside their answer, as the agent reads them into Experio_repondre's
corrections ([{facture, compte}]), measured on the real model. The cases are the email runs of 2026-10-21/22 (RB1,
X3, RB2, RC7 in .temp/first-equipment/RESULTS.md): a correction for another invoice than the one answered, named by
Tarik's number, its own number or its supplier; one for « celle-ci »; amounts and years that are no account; and
the plain answer that carries none. A correction the agent misses is lost (asked again at best); one it invents, or
gives the wrong invoice, would change an account the client never asked for: wrong.

Slow and billed: python -m pytest -m llm tests/llm/test_corrections.py -s."""

import pytest

from engine.tools.core.words import normalized
from tests.llm.test_decisions import after

MIN_RIGHT = 0.9
MAX_WRONG = 0.05

WAITING = (
    "**Questions**\n"
    "1. QUESTION : Facture 272459 : Facture de STATION SERVICE - 4411S002 : la TVA est-elle non récupérable "
    "(voyage, carburant, véhicule de tourisme) ? oui / non\n"
    "2. QUESTION : Facture 272469 : Facture 1579653218 : elle couvre 2 mois (2025-03-19 au 2025-04-18). La répartir en "
    "charges constatées d'avance ? oui / non\n"
    "3. QUESTION : Facture 272471 : Elle est datée du 2024-11-22, en dehors des exercices ouverts (Exercice 2025, "
    "Exercice 2026). Je la comptabilise dans un de ceux-là (comme facture antérieure, précisez lequel), ou je crée un "
    "nouvel exercice couvrant sa vraie date (répondez « nouveau ») ?"
)


def case(said, about, corrections):
    """The client answers ``said`` to the three questions waiting; expected: the invoice it answers (``about``) and
    the corrections [(the invoice as named, "" for the one answered; a word of the account)]."""
    given = {"dossier": "TEST FIRST EQUIPEMENT 2", "invoice_type": "achat"}
    messages = after(
        "Voici les factures d'achat du dossier TEST FIRST EQUIPEMENT 2.",
        "Experio_traiter_fichiers",
        given,
        WAITING,
        said,
    )
    expected = {"about": about, "corrections": corrections}
    return {"skill": "Réponse à une question", "messages": messages, "expected": expected, "said": said}


def alone(said, corrections):
    """The client corrects an invoice's account with nothing waiting (its invoices processed, held as drafts)."""
    given = {"dossier": "TEST FIRST EQUIPEMENT 2", "invoice_type": "achat"}
    held = (
        "**En attente**\n- Facture 272459 : importée et vérifiée, laissée en brouillon. Dites-moi quand la "
        "comptabiliser."
    )
    messages = after(
        "Voici les factures d'achat du dossier TEST FIRST EQUIPEMENT 2.", "Experio_traiter_fichiers", given, held, said
    )
    expected = {"about": "", "corrections": corrections}
    return {"skill": "Traitement des documents", "messages": messages, "expected": expected, "said": said}


def loaded(skill, call_id):
    """The agent's load_skill of ``skill`` and its result: the skill's instructions as they are now, as the agent
    shows them (engine.agent.current_skills)."""
    from engine.agent import skills

    instructions = next(text for name, _, text in skills() if name == skill)
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": "load_skill", "input": {"name": skill}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": instructions}]},
    ]


def resumed(said, corrections):
    """The client corrects an invoice's account later in the thread, as in X3: both skills loaded on earlier turns,
    its invoices answered and held as drafts, nothing waiting, the agent's very own start (no skill given)."""
    given = {"dossier": "TEST FIRST EQUIPEMENT 2", "invoice_type": "achat", "comptabiliser": "oui"}
    held = (
        "**En attente**\n- Facture 272471 : importée et vérifiée, laissée en brouillon. Dites-moi quand la "
        "comptabiliser.\n\n**À corriger**\n- Facture 272469 : Pas comptabilisable en l'état. Experio signale : la "
        "somme du HT, TVA, debours... etc. doit être égale au montant TTC."
    )
    subject = "Objet : Trois factures\n\n"
    messages = [
        {
            "role": "user",
            "content": f"{subject}Voici trois factures d'achat. Dossier : TEST FIRST EQUIPEMENT 2. Une "
            "fois vérifiées, comptabilisez-les.",
        },
        *loaded("Traitement des documents", "call_1"),
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_2", "name": "Experio_traiter_fichiers", "input": given}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_2", "content": WAITING}]},
        {"role": "assistant", "content": WAITING},
        {"role": "user", "content": f"{subject}Exercice 2025 pour la 272471, comme facture antérieure."},
        *loaded("Réponse à une question", "call_3"),
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_4",
                    "name": "Experio_repondre",
                    "input": {"decision": "Exercice 2025", "about": "272471"},
                }
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_4", "content": held}]},
        {"role": "assistant", "content": held},
        {"role": "user", "content": subject + said},
    ]
    expected = {"about": "", "corrections": corrections}
    return {"skill": None, "messages": messages, "expected": expected, "said": said, "follow": True}


CASES = [
    # Later in the thread, nothing waiting and no skill loaded this turn (X3): still Experio_repondre's corrections.
    resumed("Mettez la 272471 en compte 61330000, s'il vous plaît.", [("272471", "61330000")]),
    resumed("Finalement la 272471 va en Entretien et réparations.", [("272471", "entretien")]),
    # Nothing waiting: a correction alone goes to Experio_repondre's corrections, never the processing tool's compte.
    alone("Et mettez la 272459 en compte 61330000, s'il vous plaît.", [("272459", "61330000")]),
    alone("Ce n'est pas du téléphone : mettez la 272459 en frais de péage.", [("272459", "peage")]),
    # Another invoice's account beside the answer: by Tarik's number, its own number, its supplier's.
    case(
        "Pour la 272459 : non, la TVA est récupérable. Et mettez la 272471 en compte 61425000 Transport sur achats.",
        "272459",
        [("272471", "61425000")],
    ),
    case("Non pour Station Service, et mettez la CD241122 en 61425000.", "272459", [("cd241122", "61425000")]),
    case("Pour la 272469 : non. La F-0125-2544243 en 61455000.", "272469", [("2544243", "61455000")]),
    case("Pour la 272471 : nouveau. Et mettez la 272459 en compte 61330000.", "272471", [("272459", "61330000")]),
    case("Pour la 272469 : non. Mettez la 272471 en Entretien et réparations.", "272469", [("272471", "entretien")]),
    # The answered invoice's own (« mettez-la », « celle-ci »), alone or beside another's.
    case("Pour Station Service : non, et mettez-la en frais de péage.", "272459", [("", "peage")]),
    case(
        "Pour Station Service : non, mettez celle-ci en Entretien et réparations ; la 272471 en 61425000.",
        "272459",
        [("", "entretien"), ("272471", "61425000")],
    ),
    # No account at all: an amount, a year, a plain answer.
    case("Pour Station Service : non, elle a été réglée en 3000 DH.", "272459", []),
    case("Pour la 272459 : non, la TVA est récupérable ; elle a été payée en 3000,50 DH.", "272459", []),
    case("Exercice 2025 pour la 272471, comme facture antérieure.", "272471", []),
    case("Pour la 272459 : non, la TVA est récupérable.", "272459", []),
]


# How the invoice answered about may be named: « mettez-la », or by its number or its supplier all the same.
ANSWERED = {"272459": ("station service",), "272469": ("1579653218",), "272471": ("cd241122", "zhejiang")}


def read(call):
    """The agent's corrections as [(normalized facture, normalized compte)]."""
    found = (call or {}).get("input", {}).get("corrections") or []
    return [(normalized(str(c.get("facture") or "")), normalized(str(c.get("compte") or ""))) for c in found]


def corrections(call, expected):
    """Right: the invoice answered and every account given, nothing more. Wrong: an account the client did not give,
    or given to the wrong invoice. Asked again: one missed, or no call (the client is asked)."""
    if not call or call["name"] != "Experio_repondre":
        return "asked again" if not call else "wrong"
    got, missed = read(call), 0
    for invoice, account in expected["corrections"]:
        answered = ("", "celle ci", "la", expected["about"], *ANSWERED.get(expected["about"], ()))
        mine = [g for g in got if account in g[1] and (invoice in g[0] if invoice else g[0] in answered)]
        if mine:
            got.remove(mine[0])
        else:
            missed += 1
    if got:  # an account read where the client gave none, or for another invoice than they named
        return "wrong"
    if expected["about"] not in str(call["input"].get("about", "")) and expected["about"]:
        return "asked again"  # the answer goes nowhere: asked again
    return "asked again" if missed else "right"


@pytest.mark.llm
def test_the_accounts_given_beside_an_answer_are_read_for_the_invoice_named(measure):
    right, wrong, _ = measure(CASES, corrections)
    assert right >= MIN_RIGHT and wrong <= MAX_WRONG
