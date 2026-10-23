"""The Comptes page, an admin's: every account, one made for a colleague (with a one-time link to send them, for
them to choose their password), a role changed, an account closed, a new link (a forgotten password). And the
invitation page, open without a session: the link's person chooses their password (server/accounts.py)."""

from starlette.requests import Request
from starlette.responses import JSONResponse

from server import accounts, audit
from server.api.runs import page


async def form_of(request, most=10):
    """A form posted as JSON ({field: text}): its text fields only, {} for anything else or more than ``most``."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    if not isinstance(body, dict) or len(body) > most:
        return {}
    return {key: value for key, value in body.items() if isinstance(value, str)}


async def accounts_page(request: Request):
    """The accounts (GET), and an admin's action on one (POST, JSON: add, role, close, link), answered with the page
    again. A new link is shown once, in that answer only: it is not kept anywhere it could be read again."""
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
    """Choosing one's password through a one-time link (GET who it is for, POST the password twice as JSON), then
    to the login (the answer's ``next``)."""
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
