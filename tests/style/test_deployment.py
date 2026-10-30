"""What a deployment needs stays in .env.example: no address in the code, every environment variable the code reads
written there, and nothing in it that Vault holds (the server refuses to start with such a copy)."""

import re
from pathlib import Path

import pytest

from engine.tools.core.cabinets import misplaced_in_vault

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / ".env.example"
CODE = [*(ROOT / "engine").rglob("*.py"), *(ROOT / "server").rglob("*.py"), ROOT / "run.py"]
URL = re.compile(r"https?://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)[a-z0-9-]+(\.[a-z0-9-]+)+", re.IGNORECASE)
READ = re.compile(r"""(?:getenv|environ\.get|environ)\(?\[?\s*["']([A-Z][A-Z0-9_]+)["']""")


@pytest.mark.technical
def test_no_address_is_written_in_the_code():
    found = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in CODE
        if "node_modules" not in path.parts and "web" not in path.parts
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if URL.search(line)
    ]
    assert found == [], "an address belongs in .env.example (a variable) or in Vault, never in the code"


@pytest.mark.technical
def test_every_environment_variable_the_code_reads_is_in_env_example():
    if not EXAMPLE.exists():
        pytest.skip(".env.example is not mounted here")
    text = EXAMPLE.read_text()
    read = {name for path in CODE for name in READ.findall(path.read_text())} - {"TARIK_CABINETS"}  # the tests' only
    assert sorted(name for name in read if name not in text) == []


@pytest.mark.technical
def test_env_example_holds_no_setting_vault_holds():
    if not EXAMPLE.exists():
        pytest.skip(".env.example is not mounted here")
    lines = [line.split("=", 1)[0].strip() for line in EXAMPLE.read_text().splitlines() if "=" in line]
    names = [name for name in lines if re.fullmatch(r"[A-Z][A-Z0-9_]+", name)]
    assert misplaced_in_vault({}, {}, environ=dict.fromkeys(names, "x")) == []
