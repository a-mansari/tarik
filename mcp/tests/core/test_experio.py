"""The guards every tool goes through: a real dossier or invoice id, the exact cabinet, Tarik's own user."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.experio import exact_cabinet, expected_uid, require_dossier, require_invoice


@pytest.mark.parametrize("bad", [None, 0, -5, "778", True])
def test_a_dossier_tool_needs_a_real_dossier_id(bad):
    with pytest.raises(ToolError, match="find_dossier"):
        require_dossier(bad)


@pytest.mark.parametrize("bad", [None, 0, -1, "264417", True])
def test_an_invoice_tool_needs_a_real_invoice_id(bad):
    with pytest.raises(ToolError, match="invoice_id"):
        require_invoice(bad, ["id"])


def test_the_cabinet_database_is_used_exactly():
    assert exact_cabinet("experio_cabinet_8") == "experio_cabinet_8"
    for bad in (None, "", " experio_cabinet_8", "experio_cabinet_8 ", "experio cabinet 8", "experio_cabinet_8\n"):
        with pytest.raises(SystemExit):
            exact_cabinet(bad)


def test_tarik_user_id_is_locked():
    assert expected_uid("430") == 430
    for bad in (None, "", "0", "-1", "abc", " 430"):
        with pytest.raises(SystemExit):
            expected_uid(bad)
