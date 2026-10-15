"""The tests set their settings in the environment: Vault is left out, but by the tests that check
the running services (tests/n8n), which read their token from it."""

import pytest

from tarik.core import vault


@pytest.fixture(autouse=True)
def settings_from_the_environment(request, monkeypatch):
    if "tests/n8n/" not in str(request.path):
        monkeypatch.setattr(vault, "ENABLED", False)
