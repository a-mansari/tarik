"""A conversation holding files no step has taken makes the agent call a processing tool, however the client words
the message (n8n/patch.js: toolChoice required). The files are seeded for a dossier that does not exist, so the
run ends at the dossier check and nothing is written to Experio."""

import json
import re
import uuid

import psycopg
import pytest

from tests.n8n.test_conversation import CABINET, N8N_DB, POSTGRES, agent  # noqa: F401

PROCESSING = ("Experio_traiter_fichiers", "Experio_parametrer_dossier", "Experio_importer_dossiers")
MESSAGES = [
    "Voici les factures d'achat de Zzqxw Inconnu.",
    "Voici celles de Zzqxw Inconnu.",
    "Voilà, pouvez-vous regarder ça ?",
    "Ce sont des achats.",
    "Je vous ai envoyé mes factures, vous les avez reçues ?",
]


def tool_calls(session):
    with psycopg.connect(**N8N_DB) as db:
        rows = db.execute(
            'select role, content::text from agents_messages where "threadId" = %s order by "createdAt"', (session,)
        ).fetchall()
    last = max(i for i, (role, _) in enumerate(rows) if role == "user")
    return [
        part["toolName"]
        for _, content in rows[last:]
        for part in json.loads(content)["content"]
        if part["type"] == "tool-call"
    ]


@pytest.mark.live
@pytest.mark.parametrize("message", MESSAGES, ids=[re.sub(r"\W+", "-", m)[:30] for m in MESSAGES])
def test_files_not_taken_make_the_agent_call_a_processing_tool(n8n, agent, message):  # noqa: F811
    session = f"test-required-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(**POSTGRES, options=f"-c search_path={CABINET}") as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) values (%s, 'facture.pdf', "
            "'application/pdf', ''::bytea)",
            (session,),
        )
    try:
        n8n.ask(agent, message, session, new=True)
        assert any(name in PROCESSING for name in tool_calls(session)), tool_calls(session)
    finally:
        with psycopg.connect(**N8N_DB) as db:
            db.execute('delete from agents_messages where "threadId" = %s', (session,))
        with psycopg.connect(**POSTGRES, options=f"-c search_path={CABINET}") as db:
            for table in ("tarik_results", "tarik_client_messages", "tarik_files"):
                db.execute(f"delete from {table} where session_id = %s", (session,))
