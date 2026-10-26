"""The stack's default cabinet, and how a run's config names its cabinet and conversation. Every other setting
(Vault: the shared ones, the orchestrator's own, each cabinet's) is read through engine.tools.core.cabinets.setting,
within a cabinet."""

import os

DEFAULT_CABINET = os.getenv("TARIK_DEFAULT_CABINET", "experio_cabinet_8")


def cabinet_of(config):
    """Return the cabinet a run works for: config.configurable.cabinet, else DEFAULT_CABINET."""
    return (config or {}).get("configurable", {}).get("cabinet") or DEFAULT_CABINET


def session_of(config):
    """Return the conversation's id a run works for: configurable.session_id, else configurable.thread_id, else "".

    session_id comes first because the server's thread ids must be UUIDs and an email thread's session is not."""
    configurable = (config or {}).get("configurable", {})
    return configurable.get("session_id") or configurable.get("thread_id", "")
