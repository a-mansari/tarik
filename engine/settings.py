"""Where this stack finds its content and its default cabinet. Every setting (Vault: the shared ones, the
orchestrator's own, each cabinet's) is read through tarik.core.cabinets.setting, within a cabinet."""

import os

DEFAULT_CABINET = os.getenv("TARIK_DEFAULT_CABINET", "experio_cabinet_8")


def cabinet_of(config):
    """The cabinet a run works for: config.configurable.cabinet, else the default one."""
    return (config or {}).get("configurable", {}).get("cabinet") or DEFAULT_CABINET
