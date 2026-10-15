"""Tarik keeps what the client said earlier in the same conversation: measured on a cabinet's real
agent with the real model. No file is sent, so the processing finds nothing to do: no Experio change.

Needs n8n's MCP token (tests/n8n/conftest.py) and the agent of TARIK_TEST_CABINET (default
experio_cabinet_8), its AGENT_ID in Vault. Slow (about a minute a conversation) and costs model calls.
Run: docker compose exec -T tarik-mcp python -m pytest tests/n8n/test_conversation.py"""

import json
import os
import re
import uuid

import psycopg
import pytest

from tarik.core import vault
from tarik.core.cabinets import POSTGRES, using
from tarik.dossiers.search import dossiers, normalized

CABINET = os.getenv("TARIK_TEST_CABINET", "experio_cabinet_8")
N8N_DB = {**POSTGRES, "dbname": os.getenv("N8N_POSTGRES_DB", "n8n")}

# The earlier message says something the last one relies on; in the last turn, Experio_traiter_fichiers
# must be called with this dossier (by name, found by the agent) and this invoice_type.
SCENARIOS = [
    (["Voici les factures de Gecafisc.", "Ce sont des achats."], "gecafisc", "achat"),
    (["Voici mes factures d'achat.", "Gecafisc."], "gecafisc", "achat"),
    (["Pour toute cette conversation, les factures que je vous envoie sont des ventes.", "Voici celles de KTSYS."], "ktsys", "vente"),
    (["Je travaille sur le dossier DJ BUILDING aujourd'hui.", "Voici les factures fournisseurs."], "dj building", "achat"),
    (["Voici les factures d'achat de la Pharmacie Jawad.", "Et maintenant les ventes du même dossier."], "pharmacie jawad", "vente"),
    (["Voici les relevés de CAFE EPSOM.", "Traitez aussi les fichiers que je vous ai envoyés tout à l'heure."], "cafe epsom", ""),
    (["Le dossier dont je parle, c'est KTSYS.", "Ce sont des factures d'achat, traitez-les."], "ktsys", "achat"),
]


@pytest.fixture(scope="module")
def agent():
    agent_id = vault.settings()[1].get(CABINET, {}).get("AGENT_ID") if vault.ENABLED else os.getenv("TARIK_TEST_AGENT_ID")
    if not agent_id:
        pytest.skip(f"no agent for {CABINET}")
    return agent_id


@pytest.fixture(scope="module")
def dossier_ids(n8n, agent):
    """The cabinet's dossiers' ids, by normalized name."""
    with using(CABINET):
        return {normalized(d["name"]): d["id"] for d in dossiers()}


@pytest.fixture
def conversation(n8n, agent):
    """Talk to the agent in a fresh conversation; its rows are deleted afterwards."""
    session = f"test-conversation-{uuid.uuid4().hex[:8]}"

    def talk(turns):
        for index, message in enumerate(turns):
            n8n.ask(agent, message, session, new=index == 0)
        with psycopg.connect(**N8N_DB) as db:
            rows = db.execute('select role, content::text from agents_messages where "threadId" = %s order by "createdAt"',
                              (session,)).fetchall()
        last = max(i for i, (role, _) in enumerate(rows) if role == "user")
        last_turn = "\n".join(content for _, content in rows[last:])
        return [json.loads(m.group(1)) for m in re.finditer(r'"toolName":\s*"Experio_traiter_fichiers",\s*"input":\s*(\{[^{}]*\})', last_turn)]
    yield talk
    with psycopg.connect(**N8N_DB) as db:
        db.execute('delete from agents_messages where "threadId" = %s', (session,))
    with psycopg.connect(**POSTGRES, options=f"-c search_path={CABINET}") as db:
        db.execute("delete from tarik_results where session_id = %s", (session,))
        db.execute("delete from tarik_client_messages where session_id = %s", (session,))


@pytest.mark.live
@pytest.mark.parametrize("turns, dossier, invoice_type", SCENARIOS, ids=[s[0][-1][:40] for s in SCENARIOS])
def test_the_last_message_is_handled_with_what_was_said_before(conversation, dossier_ids, turns, dossier, invoice_type):
    calls = conversation(turns)
    assert len(calls) == 1, calls
    assert int(float(calls[0].get("dossier_id") or 0)) == dossier_ids[dossier], calls
    assert (calls[0].get("invoice_type") or "") == invoice_type, calls
