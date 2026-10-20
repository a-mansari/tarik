"""Who did what in the UI, in one journal (table audit, the Journal page, an admin's): logging in and out (a failed
try too), the accounts made, closed, given a role or a new link, a password set through a link, the content and
the cabinets' settings changed (each also keeps its own history), a canvas's layout saved or forgotten, a run
started by hand. Recorded once the action is done; a journal that cannot be written is logged, never the action
undone. A secret never goes in: a password, a link's token, a setting's value only for what is not secret (as
settings_changes keeps it)."""

from psycopg.types.json import Jsonb

from engine import runs
from tarik.core.app import log
from tarik.core.queries import Q

ACTIONS = (
    "login",
    "login_failed",
    "logout",
    "account_made",
    "account_invited",
    "account_link",
    "account_role",
    "account_closed",
    "password_set",
    "content_saved",
    "settings_saved",
    "layout_saved",
    "layout_reset",
    "run_started",
)


async def record(actor, action, target="", **detail):
    """One entry: ``actor`` did ``action`` (one of ACTIONS) on ``target``, with ``detail``."""
    assert action in ACTIONS, action
    try:
        async with runs.pool.connection() as db:
            await db.execute(Q.audit_recorded, (actor or "?", action, str(target), Jsonb(detail)))
    except Exception as error:  # the action is done: its trace missing is logged, never the action undone
        log(f"audit: {action} by {actor} on {target} not recorded: {type(error).__name__}")


async def listed(action=None, actor=None, before=None, limit=100):
    """The latest entries (before id ``before``), of one action or one actor when given: [{id, at, actor, action,
    target, detail}]."""
    asked = {"action": action or None, "actor": actor or None, "before": before or 2**62, "limit": limit}
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.audit_listed, asked)).fetchall()
        actors = [row[0] for row in await (await db.execute(Q.audit_actors)).fetchall()]
    keys = ("id", "at", "actor", "action", "target", "detail")
    return [dict(zip(keys, row, strict=True)) for row in rows], actors
