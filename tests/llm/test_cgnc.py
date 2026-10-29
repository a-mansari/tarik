"""The CGNC in Tarik's answers, measured on the real model shown the agent as it starts (no skill loaded): an
accounting question goes first to Experio_consult_cgnc; the CGNC's pieces given, the answer names the account they
give and cites the CGNC with its page; and a tax question (TVA rates, withholding) never gets a rate drawn from the
CGNC, which has none. The tool's answers are the real ones (engine/knowledge/cgnc.py), read from the stack's
schema knowledge (read only); a CGNC search the model makes on the way runs for real, as the agent's would.

Slow and billed: python -m pytest -m llm -s tests/llm/test_cgnc.py (-s shows the cases missed and the scores)."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from engine import agent as tarik
from engine.knowledge.cgnc import consult_cgnc
from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import using
from engine.tools.core.words import normalized
from tests.llm.conftest import CABINET, REPEAT

MIN_RIGHT = 0.9
TOOL = "Experio_consult_cgnc"
TURNS = 4  # the model's answers in one case at most: up to 3 searches, then its reply

# An accounting question, as clients write them: the agent calls the CGNC before it answers.
QUESTIONS = [
    "Sur quel compte j'enregistre les factures d'eau et d'électricité ?",
    "Les fournitures de bureau qu'on ne stocke pas, je les mets où ?",
    "Comment comptabiliser les honoraires de notre avocat ?",
    "Le loyer du local commercial, c'est quel compte ?",
    "On a acheté deux ordinateurs portables pour le bureau, quel compte ?",
    "À la clôture, une assurance payée d'avance pour l'an prochain, je fais quoi ?",
    "C'est quoi le compte 6125 ?",
    "Que dit le CGNC sur le compte 4455 ?",
    "Les frais de réparation d'un véhicule de société, compte de charge ?",
    "Comment enregistrer une subvention d'exploitation reçue ?",
]

# The CGNC searched with ``given`` first: the reply names one of the accounts and cites the CGNC with a page.
ANSWERS = [
    ("Sur quel compte j'enregistre les factures d'eau et d'électricité ?", "eau électricité", {"6125", "61251"}),
    ("Les fournitures de bureau non stockées, quel compte ?", "fournitures de bureau", {"6125", "61254"}),
    ("Comment comptabiliser les honoraires de notre avocat ?", "honoraires", {"6136", "61365"}),
    ("Le loyer du local commercial, c'est quel compte ?", "locations charges locatives", {"6131", "61313"}),
    ("On a acheté deux ordinateurs pour le bureau, quel compte ?", "matériel informatique", {"2355"}),
    ("Une assurance payée d'avance pour l'an prochain ?", "charges constatées d'avance", {"3491"}),
]

# A tax question, the CGNC searched first: no rate in the reply, the CGNC has none (they are the CGI's).
TAXES = [
    ("Quel est le taux de TVA sur les honoraires d'un avocat ?", "TVA honoraires"),
    ("Quel taux de retenue à la source sur les honoraires versés à un non-résident ?", "retenue à la source"),
    ("La TVA sur le gasoil est récupérable à combien ?", "TVA récupérable"),
]
RATE = re.compile(r"\d+(?:[.,]\d+)? ?%")


def first_move(call, expected):
    if not call or call["name"] != expected:
        return "wrong"  # no tool: answered from what the model knows, not from the CGNC
    return "right" if (call["input"].get("code") or call["input"].get("words")) else "wrong"


@pytest.mark.llm
def test_an_accounting_question_goes_first_to_the_cgnc(measure):
    cases = [
        {"skill": None, "said": said, "messages": [{"role": "user", "content": said}], "expected": TOOL}
        for said in QUESTIONS
    ]
    right, _, _ = measure(cases, first_move)
    assert right >= MIN_RIGHT


def ran(call):
    """The tool_result of ``call``: the CGNC's real answer, as the agent gets it (untrusted data), for a CGNC
    search; « Aucun résultat » for any other tool."""
    found = "Aucun résultat"
    if call["name"] == TOOL:
        given = {key: str(value) for key, value in call["input"].items() if key in ("code", "words")}
        try:
            with using(CABINET):
                found = tarik.untrusted(consult_cgnc(**given), TOOL)
        except ToolError as refusal:
            found = str(refusal)
    return {"type": "tool_result", "tool_use_id": call["id"], "content": found}


def searched(said, words):
    """The conversation: the client's question ``said``, the agent's CGNC search for ``words`` and its answer."""
    call = {"type": "tool_use", "id": "call_1", "name": TOOL, "input": {"words": words}}
    return [
        {"role": "user", "content": said},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [ran(call)]},
    ]


def replies(ask, cases):
    """Each case (said, messages) played REPEAT times, 3 at once, as the agent would: each tool the model calls
    answered (ran), until it replies (TURNS at most). Returns [(said, its reply, every tool result it read)]."""

    def played(case):
        said, messages = case
        messages = list(messages)
        for _ in range(TURNS):
            content = ask(None, messages)[0]["content"]
            calls = [block for block in content if block["type"] == "tool_use"]
            if not calls:
                break
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": list(map(ran, calls))}]
        reply = "\n".join(block["text"] for block in content if block["type"] == "text")
        results = [part for m in messages if isinstance(m["content"], list) for part in m["content"]]
        return said, reply, "\n".join(part["content"] for part in results if part["type"] == "tool_result")

    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(played, [case for case in cases for _ in range(REPEAT)]))


def uncited(reply, read, accounts):
    """Why ``reply`` misses: no account of ``accounts``, no CGNC named, or no page of what it ``read``; "" when it
    cites."""
    if not any(re.search(rf"(?<!\d){code}(?!\d)", reply) for code in accounts):
        return f"no account among {sorted(accounts)}"
    if "cgnc" not in normalized(reply):
        return "the CGNC not named"
    if not set(re.findall(r"p\. (\d+)", read)) & set(re.findall(r"(?<!\d)(\d{1,3})(?!\d)", reply)):
        return "no page of the CGNC's pieces"
    return ""


@pytest.mark.llm
def test_the_answer_names_the_account_and_cites_the_cgnc(ask):
    accounts = {said: expected for said, _, expected in ANSWERS}
    told = replies(ask, [(said, searched(said, words)) for said, words, _ in ANSWERS])
    misses = [(said, why, reply) for said, reply, read in told if (why := uncited(reply, read, accounts[said]))]
    for said, why, reply in misses:
        print(f"wrong: « {said} »: {why}\n    {reply[:300]!r}")
    right = 1 - len(misses) / len(told)
    print(f"{len(told)} answers: {right:.0%} cite the CGNC with the account")
    assert right >= MIN_RIGHT


@pytest.mark.llm
def test_a_tax_rate_is_never_drawn_from_the_cgnc(ask):
    told = replies(ask, [(said, searched(said, words)) for said, words in TAXES])
    misses = [(said, reply) for said, reply, _ in told if RATE.search(reply)]
    for said, reply in misses:
        print(f"wrong: « {said} » gave a rate\n    {reply[:300]!r}")
    print(f"{len(told)} tax answers: {len(misses)} with a rate")
    assert not misses
