"""python -m pytest-free check of the dossier matching: python test_server.py (inside the tarik-mcp container)."""

import asyncio

from mcp.server.mcpserver.exceptions import ToolError

from server import MIN_SIMILARITY, require_dossier, server, similarity

DOSSIERS = ["STE ALLIANCE GENIE CIVIL SARL", "Alliance Transport", "Boulangerie Atlas", "ATLAS IMMOBILIER", "Cabinet Benali"]


def best(query):
    return max(DOSSIERS, key=lambda name: similarity(query, name))


assert best("alliance genie civil") == "STE ALLIANCE GENIE CIVIL SARL"
assert best("Alliance Génie-Civil") == "STE ALLIANCE GENIE CIVIL SARL"  # accents, hyphen
assert best("boulangeri atlas") == "Boulangerie Atlas"  # typo
assert best("immobilier atlas") == "ATLAS IMMOBILIER"  # word order
assert best("benali") == "Cabinet Benali"  # part of the name
assert similarity("benali", "Cabinet Benali") >= 0.9
assert max(similarity("xyz", name) for name in DOSSIERS) < MIN_SIMILARITY  # nothing alike
assert similarity("zzqx boulangerie inexistante", "FinStrat Consulting") < MIN_SIMILARITY  # was a 51 % false match
assert similarity("", "Atlas") == 0.0

# A dossier tool fails without a real dossier id, before touching Experio or the file.
for bad in (None, 0, -5, "778", True):
    try:
        require_dossier(bad)
    except ToolError as error:
        assert "find_dossier" in str(error)
    else:
        raise AssertionError(f"require_dossier accepted {bad!r}")
for args in ({"file_id": 1}, {"file_id": 1, "dossier_id": None}, {"file_id": 1, "dossier_id": 0}):
    try:
        asyncio.run(server.call_tool("create_draft_invoice", args))
    except ToolError as error:  # a ToolError reaches the agent with its message
        assert "dossier_id" in str(error), error
    else:
        raise AssertionError(f"create_draft_invoice accepted {args}")
print("similarity and dossier checks ok")
