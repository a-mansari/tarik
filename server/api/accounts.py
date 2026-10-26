"""Serve the Comptes page (admins only) and the invitation page.

Comptes lists every account and lets an admin create one for a colleague (with a one-time link to send them, to
choose their password), change a role, close an account, or make a new link (a forgotten password). The invitation
page, open without a session, lets the link's person choose their password (server/accounts.py)."""

from starlette.requests import Request
from starlette.responses import JSONResponse

from server import accounts, audit
from server.api.runs import page


async def form_of(request, most=10):
    """Return the JSON form posted in ``request``, keeping only its string fields; {} when it is not a JSON object or
    has more than ``most`` fields."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    if not isinstance(body, dict) or len(body) > most:
        return {}
    return {key: value for key, value in body.items() if isinstance(value, str)}


async def accounts_page(request: Request):
    """GET or POST /ui/api/comptes: return the accounts, after applying an admin's action when posted.

    POST takes JSON {action: "add" | "role" | "close" | "link", email, name?, role?}; each action is recorded in the
    Journal. A new link appears only in this response (``link``): it is stored nowhere it could be read again. Status
    400 when the action is refused."""
    done, error, link = None, None, None
    if request.method == "POST":
        form = await form_of(request)
        action, email = form.get("action"), form.get("email", "")
        me = request.state.user["email"]
        try:
            if action == "add":
                role = form.get("role", "member")
                link = await accounts.invite(email, form.get("name", ""), role)
                done = f"Compte créé pour {email.strip().lower()}."
                await audit.record(me, "account_invited", email.strip().lower(), role=role)
            elif action == "link":
                link = await accounts.new_link(email)
                done = f"Nouveau lien pour {email}."
                await audit.record(me, "account_link", email)
            elif action == "role":
                await accounts.set_role(email, form.get("role", ""))
                await audit.record(me, "account_role", email, role=form.get("role", ""))
                done = f"{email} est maintenant {'administrateur' if form.get('role') == 'admin' else 'membre'}."
            elif action == "close":
                await accounts.disable(email)
                await audit.record(me, "account_closed", email)
                done = f"Compte de {email} fermé."
            else:
                error = "action inconnue"
        except (ValueError, LookupError) as refused:
            error = str(refused)
    context = {
        "accounts": await accounts.listed(),
        "done": done,
        "error": error,
        "link": f"{str(request.base_url).rstrip('/')}/invitation/{link}" if link else None,
        "hours": accounts.INVITATION_HOURS,
        "me": request.state.user["email"],
    }
    return page(context, 400 if error else 200)


async def invitation_page(request: Request):
    """GET or POST /ui/api/invitation/{token}: show who a one-time link is for, or set the password chosen through it.

    POST takes JSON {password, again} and must come from these pages (403 otherwise). On success returns {next:
    "/login?ready=1"}; 400 when the passwords differ or are refused; 404 when the link is unknown, used or expired."""
    token = request.path_params["token"]
    found = await accounts.invited(token)
    if found is None:
        return page({"gone": True}, 404)
    error = None
    if request.method == "POST":
        from server.server import same_origin  # the server imports these pages

        if not same_origin(request):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        form = await form_of(request)
        if form.get("password", "") != form.get("again", ""):
            error = "les deux mots de passe diffèrent"
        else:
            try:
                email = await accounts.accept(token, form.get("password", ""))
                await audit.record(email, "password_set", email, via="lien")
                return page({"next": "/login?ready=1"})
            except ValueError as refused:
                error = str(refused)
    context = {"found": found, "error": error, "min": accounts.MIN_PASSWORD}
    return page(context, 400 if error else 200)
