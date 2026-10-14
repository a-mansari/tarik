"""python -m pytest-free check of the dossier matching: python test_server.py (inside the tarik-mcp container)."""

import asyncio

from mcp.server.mcpserver.exceptions import ToolError

import io

import pypdfium2
from PIL import Image

from server import MIN_SIMILARITY, exact_cabinet, expected_uid, split_pdf, require_dossier, server, similarity

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

# The cabinet's database name is used exactly, or Tarik doesn't start.
assert exact_cabinet("experio_cabinet_8") == "experio_cabinet_8"
for bad in (None, "", " experio_cabinet_8", "experio_cabinet_8 ", "experio cabinet 8", "experio_cabinet_8\n"):
    try:
        exact_cabinet(bad)
    except SystemExit:
        continue
    raise AssertionError(f"exact_cabinet accepted {bad!r}")
assert expected_uid("430") == 430
for bad in (None, "", "0", "-1", "abc", " 430"):
    try:
        expected_uid(bad)
    except SystemExit:
        continue
    raise AssertionError(f"expected_uid accepted {bad!r}")

# Cutting a 4-page PDF where invoices start on pages 1 and 3.
buffer = io.BytesIO()
pages = [Image.new("RGB", (200, 280), color) for color in ("white", "gray", "white", "gray")]
pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
four = buffer.getvalue()
parts = split_pdf(four, [1, 3])
assert [(first, last) for first, last, _ in parts] == [(1, 2), (3, 4)]
assert [len(pypdfium2.PdfDocument(raw)) for _, _, raw in parts] == [2, 2]
assert [(f, l) for f, l, _ in split_pdf(four, [3, 0, 9, 3])] == [(1, 2), (3, 4)]  # page 1 added, out of range dropped
assert [(f, l) for f, l, _ in split_pdf(four, [])] == [(1, 4)]
assert len(split_pdf(four, [1, 2, 3, 4])) == 4
print("similarity, dossier, cabinet and split checks ok")
