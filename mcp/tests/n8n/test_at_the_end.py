"""n8n still runs the node made by Workflow.at_the_end after every other one, wherever it sits in the editor:
"Résumé" relies on it in Traitement, Dossier and Modification. Runs "Tarik - Test Merge" (TarikMergeTest01,
workflows/tarik-merge-test.json) in the real n8n; re-run after each n8n upgrade.

Needs n8n's MCP token (tests/n8n/conftest.py). Run: docker compose exec -T tarik-mcp python -m pytest -m live
tests/n8n/test_at_the_end.py"""

import time

import pytest

WORKFLOW = "TarikMergeTest01"


@pytest.mark.live
def test_the_last_node_runs_after_every_other(n8n):
    execution_id = str(n8n.tool("execute_workflow", workflowId=WORKFLOW, executionMode="manual")["executionId"])
    for _ in range(60):
        found = n8n.tool("get_workflow_execution", workflowId=WORKFLOW, executionId=execution_id, includeData=True)
        if found["execution"]["status"] not in ("new", "running", "waiting"):
            break
        time.sleep(1)
    assert found["execution"]["status"] == "success", found["execution"]
    runs = found["data"]["resultData"]["runData"]
    order = [name for _, name in sorted((run["executionIndex"], name) for name, ran in runs.items() for run in ran)]
    # Every branch ran, the failing one included, and the last node last.
    assert set(order) >= {"Pair", "Deuxieme niveau", "Impair", "Apres l'erreur"}, order
    assert order[-2:] == ["Fin des etapes", "Dernier"], order
