"""Record who did what in the UI, in one journal (table audit, shown on the admins' Journal page).

Recorded actions (ACTIONS): logging in and out (failed attempts too); accounts made, closed, given a role or a new
link; a password set through a link; content and cabinet settings changed (each also keeps its own history); a
routine made, changed or deleted; a canvas layout saved or reset; a run started by hand; cabinets created,
reactivated or closed. An entry is written once the action is done; when the journal cannot be written, the
failure is logged and the action is not undone. A secret never goes in: no password, no link token, and a setting's
value only when it is not secret (as settings_changes keeps it)."""

from psycopg.types.json import Jsonb

from engine import runs
from engine.tools.core.app import log
from engine.tools.core.queries import Q

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
    "routine_saved",
    "layout_saved",
    "layout_reset",
    "run_started",
    "cabinet_created",
    "cabinet_reactivated",
    "cabinet_closed",
)


async def record(actor, action, target="", **detail):
    """Record that ``actor`` did ``action`` (one of ACTIONS) on ``target``, with ``detail`` (stored as JSON); a write
    failure is only logged."""
    assert action in ACTIONS, action
    try:
        async with runs.pool.connection() as db:
            await db.execute(Q.audit_recorded, (actor or "?", action, str(target), Jsonb(detail)))
    except Exception as error:  # the action is done: its trace missing is logged, never the action undone
        log(f"audit: {action} by {actor} on {target} not recorded: {type(error).__name__}")


async def listed(action=None, actor=None, before=None, limit=100):
    """Return the latest journal entries and the list of actors, as (entries, actors).

    entries: [{id, at, actor, action, target, detail}], newest first, at most ``limit``, older than entry id
    ``before`` when given, filtered by ``action`` or ``actor`` when given. actors: every actor in the journal, for
    the page's filter."""
    asked = {"action": action or None, "actor": actor or None, "before": before or 2**62, "limit": limit}
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.audit_listed, asked)).fetchall()
        actors = [row[0] for row in await (await db.execute(Q.audit_actors)).fetchall()]
    keys = ("id", "at", "actor", "action", "target", "detail")
    return [dict(zip(keys, row, strict=True)) for row in rows], actors
