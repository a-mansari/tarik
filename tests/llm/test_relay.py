"""A long run's summary as the agent passes it on to the client, on the real model: the summary as it was (every
step's line) against the one now (only what the client needs; tests/runs.py's ten invoices). The reply must tell
the client every question, stop and problem, what changed and each invoice posted (FACTS, whatever its wording:
the model often rewrites rather than copies); the shorter summary must do at least as well, and both replies are
measured: copied word for word or not, length, seconds, tokens.

Slow and billed: python -m pytest -m llm -s."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from engine.tools.core.words import normalized
from tests import runs
from tests.llm.conftest import REPEAT

# What each reply must tell the client, whatever its wording: every word of a fact, in the reply (normalized).
FACTS = {
    "the tiers question of invoice 26": ["facture 26", "atlas de roulement", "atlas transport"],
    "the duplicate 27, of draft 22": ["27", "doublon", "22"],
    "the withholding question of invoice 29": ["29", "retenue", "sans retenue", "10"],
    "the check that failed on invoice 30": ["30", "verification"],
    "invoice 28's TVA non recuperable": ["28", "non recuperable"],
    "invoice 28's new exercice 2025": ["exercice 2025"],
    **{f"invoice {invoice_id} posted": [f"facture {invoice_id}", "comptabilis"] for invoice_id in runs.POSTED},
}


def relay(ask, summary):
    """The agent's reply to the client once Experio_traiter_fichiers answered ``summary``: (text, seconds, usage)."""
    called = {"dossier": "Gecafisc", "invoice_type": "achat"}
    messages = [
        {"role": "user", "content": "Voici les factures d'achat de GECAFISC."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "Experio_traiter_fichiers", "input": called}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": summary}]},
    ]
    answer, seconds = ask("Traitement des documents", messages)
    text = "".join(block.get("text", "") for block in answer["content"] if block["type"] == "text")
    return text, seconds, answer["usage"]


def scored(text):
    """What a reply kept: (share of FACTS told, share of the questions, stops and problems copied word for word)."""
    words = normalized(text)
    told = [all(word in words for word in found) for found in FACTS.values()]
    copied = [normalized(message) in words for message in runs.kept()]
    return sum(told) / len(told), sum(copied) / len(copied)


@pytest.mark.llm
def test_the_shorter_summary_reaches_the_client_whole(ask):
    summaries = {"before": runs.summary(routine=False), "now": runs.summary()}
    jobs = [(name, summary) for name, summary in summaries.items() for _ in range(REPEAT)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(lambda job: (job[0], *relay(ask, job[1])), jobs))
    results = {}
    for name in summaries:
        mine = [(scored(text), text, seconds, usage) for kind, text, seconds, usage in replies if kind == name]
        results[name] = {
            "facts told": sum(score[0] for score, *_ in mine) / len(mine),
            "copied word for word": sum(score[1] for score, *_ in mine) / len(mine),
            "reply characters": sum(len(text) for _, text, _, _ in mine) / len(mine),
            "seconds": sum(seconds for _, _, seconds, _ in mine) / len(mine),
            "input tokens": sum(  # cached ones included: MiniMax counts them apart
                usage["input_tokens"] + (usage.get("cache_read_input_tokens") or 0) for *_, usage in mine
            )
            / len(mine),
            "output tokens": sum(usage["output_tokens"] for *_, usage in mine) / len(mine),
        }
        print(f"\n--- one reply to the {name} summary ---\n{mine[0][1]}")
        print(
            f"\n{name}: summary {len(summaries[name])} characters; "
            + ", ".join(
                f"{key} {value:.0%}" if key in ("facts told", "copied word for word") else f"{key} {value:.0f}"
                for key, value in results[name].items()
            )
        )
    now, before = results["now"], results["before"]
    assert now["facts told"] >= 0.95  # what the client needs reaches them
    assert now["facts told"] >= before["facts told"] - 0.05  # never worse than the long summary
