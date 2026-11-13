"""The settings every cabinet shares, as the general settings page shows them: the environment's, read only, a secret
only as set or not, and the memory's failures in words."""

import asyncio

from server import shared_settings


def test_every_shared_setting_is_shown():
    assert set(shared_settings.FIELDS) == shared_settings.cabinets.SHARED_SETTINGS


def test_a_secret_is_only_shown_as_set_or_not(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.old.ma")
    monkeypatch.setenv("MODEL_API_KEY", "old-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    shown = shared_settings.current()
    assert shown["MODEL_API_KEY"] is True and shown["CLAUDE_CODE_OAUTH_TOKEN"] is False
    assert shown["IMAP_HOST"] == "imap.old.ma" and "old-key" not in repr(shown)


def test_the_page_shows_the_memory_s_failures_in_words_or_that_it_cannot_be_reached(monkeypatch):
    from server.api import settings

    async def failed():
        return [
            {
                "cabinet": "experio_cabinet_8",
                "at": "2026-11-10T12:42:29+00:00",
                "kind": "retain",
                "error": "token revoked",
            },
            {"cabinet": "experio_cabinet_8", "at": None, "kind": "", "error": ""},  # no time given: not a crash
        ]

    monkeypatch.setattr(settings.memory, "URL", "http://hindsight:8888")
    monkeypatch.setattr(settings.memory, "failures", failed)
    shown = asyncio.run(settings.memory_state())
    assert shown["on"] is True and shown["failures"][0]["error"] == "token revoked"
    assert shown["failures"][0]["at"] == settings.runs.when("2026-11-10T12:42:29+00:00")  # in words, as every page
    assert shown["failures"][1]["at"] == ""

    async def down():
        return None

    monkeypatch.setattr(settings.memory, "failures", down)
    assert asyncio.run(settings.memory_state())["failures"] is None
