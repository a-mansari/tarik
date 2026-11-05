"""Serve the WhatsApp page (admins only): which of a cabinet's collaborators write to Tarik on WhatsApp, and enroll or
remove one (engine/tools/core/sparrow.py, handled by server/api/whatsapp.py).

A collaborator is enrolled with the phone number of their Experio user: Sparrow registers it with Tarik's own public
host (the Gladden callback's, the one the funnel already opens), a welcome message goes to the person, and the
recipient_id Sparrow made is kept for the cabinet and the person (whatsapp_recipients). The conversation of their
messages is a new one, wa-<uuid>."""

import uuid
from urllib.parse import urlparse

from starlette.requests import Request

from engine import runs
from engine.calls import limited
from engine.tools.core import sparrow
from engine.tools.core.cabinets import setting
from engine.tools.core.queries import Q
from engine.tools.dossiers.people import person_to_enroll, whatsapp_candidates
from server import audit
from server.api.accounts import form_of
from server.api.runs import chosen, page

NO_HOST = "L'adresse publique de Tarik n'est pas réglée (GLADDEN_CALLBACK_URL) : Sparrow ne saurait où lui écrire."
SPARROW = {
    422: "Sparrow a refusé le numéro.",
    502: "WhatsApp a refusé le message de bienvenue : le numéro n'a peut-être pas WhatsApp.",
}


def public_host():
    """Return the public host Sparrow calls Tarik on (``host_uri``: no scheme, no path), taken from the Gladden callback
    URL; raises ValueError when it is not set. Runs in the cabinet (settings are read there)."""
    try:
        host = urlparse(setting("GLADDEN_CALLBACK_URL")).netloc
    except KeyError:
        host = ""
    if not host:
        raise ValueError(NO_HOST)
    return host


def refusal(error):
    """Return a Sparrow failure in plain French for the page (the number taken, or Sparrow's status)."""
    if isinstance(error, sparrow.PhoneTaken):
        return "Ce numéro est déjà enregistré chez Sparrow : sans doute le WhatsApp d'un dossier ou d'un collaborateur."
    if str(error) == "not a Moroccan phone number":
        return "Sparrow n'accepte que les numéros marocains (+212 ou 06…)."
    return SPARROW.get(error.status, "Sparrow n'a pas répondu comme attendu : réessayez dans un instant.")


async def enroll(cabinet, login):
    """Enroll the collaborator ``login`` of ``cabinet``: their Experio phone registered at Sparrow with Tarik's host and
    kept with a new conversation. Returns their name. Raises ValueError (plain French) when it cannot be done; what
    Sparrow registered is removed again when the row cannot be kept."""
    async with runs.pool.connection() as db:
        if await (await db.execute(Q.whatsapp_person, (cabinet, login))).fetchone():
            raise ValueError(f"{login} est déjà activé sur WhatsApp")
    who = await limited(cabinet, person_to_enroll, login)
    host = await limited(cabinet, public_host)
    phone = sparrow.moroccan(who["phone"])
    if phone is None:
        raise ValueError(f"Le numéro de {who['name']} dans Experio ({who['phone']}) n'est pas un numéro marocain")
    try:
        recipient_id = await limited(cabinet, sparrow.enroll, phone, host, who["name"], who["cabinet_name"])
    except sparrow.SparrowError as error:
        raise ValueError(refusal(error)) from None
    try:
        async with runs.pool.connection() as db:
            row = (recipient_id, cabinet, who["login"], phone, f"wa-{uuid.uuid4().hex}")
            await db.execute(Q.whatsapp_enrolling, row)
    except Exception:
        await limited(cabinet, sparrow.remove, recipient_id)
        raise
    return who["name"]


async def remove(cabinet, login):
    """Remove the enrolled person ``login`` of ``cabinet``: their recipient at Sparrow, then their row. Raises
    ValueError when they are not enrolled or Sparrow cannot remove them (the row is then kept)."""
    async with runs.pool.connection() as db:
        found = await (await db.execute(Q.whatsapp_person, (cabinet, login))).fetchone()
    if not found:
        raise ValueError(f"{login} n'est pas activé sur WhatsApp")
    try:
        await limited(cabinet, sparrow.remove, found[0])
    except sparrow.SparrowError as error:
        raise ValueError(refusal(error)) from None
    async with runs.pool.connection() as db:
        await db.execute(Q.whatsapp_unenrolled, (found[0],))


async def people_of(cabinet):
    """Return the page's people: [{login, name, phone, state}] by name, state "enrolled", "ready" (a Moroccan phone to
    use), "no_phone" or "foreign" (a number Sparrow refuses). An enrolled person who is no longer a collaborator stays
    listed, to be removed."""
    candidates = await limited(cabinet, whatsapp_candidates)
    async with runs.pool.connection() as db:
        enrolled = dict(await (await db.execute(Q.whatsapp_enrolled, (cabinet,))).fetchall())
    people = []
    for person in candidates:
        if person["login"] in enrolled:
            state = "enrolled"
        elif not person["phone"]:
            state = "no_phone"
        else:
            state = "ready" if sparrow.moroccan(person["phone"]) else "foreign"
        people.append({**person, "state": state})
    left = set(enrolled) - {person["login"] for person in candidates}
    people += [{"login": login, "name": login, "phone": enrolled[login], "state": "enrolled"} for login in sorted(left)]
    return people


async def enrollment_page(request: Request):
    """GET or POST /ui/api/whatsapp: return the cabinet chosen in the sidebar with its collaborators and who of them
    writes to Tarik on WhatsApp, after applying an admin's action when posted.

    POST takes JSON {cabinet, action: "enroll" | "remove", login}; refused (400) when the sidebar has chosen another
    cabinet since the page opened, or when the action cannot be done; each success is recorded in the Journal. 404
    without any cabinet; 503 when Experio cannot be read."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    done, error = None, None
    if request.method == "POST":
        form = await form_of(request)
        action, login = form.get("action"), form.get("login", "")
        me = request.state.user["email"]
        try:
            if form.get("cabinet") != cabinet:
                raise ValueError("un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la")
            if action == "enroll":
                name = await enroll(cabinet, login)
                await audit.record(me, "whatsapp_enrolled", login, cabinet=cabinet)
                done = f"{name} peut écrire à Tarik sur WhatsApp : un message de bienvenue lui est envoyé."
            elif action == "remove":
                await remove(cabinet, login)
                await audit.record(me, "whatsapp_removed", login, cabinet=cabinet)
                done = f"{login} n'écrit plus à Tarik sur WhatsApp."
            else:
                raise ValueError("action inconnue")
        except ValueError as refused:
            error = str(refused)
    try:
        people = await people_of(cabinet)
    except Exception:  # Experio cannot be read: the page says so, nothing guessed
        return page({"cabinet": cabinet, "unreachable": True}, 503)
    return page({"cabinet": cabinet, "people": people, "done": done, "error": error}, 400 if error else 200)
