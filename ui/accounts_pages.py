"""The Comptes page, an admin's: every account, one made for a colleague (with a one-time link to send them, for
them to choose their password), a role changed, an account closed, a new link (a forgotten password). And the
invitation page, open without a session: the link's person chooses their password (app/accounts.py)."""

from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app import accounts, audit
from ui.pages import templates


def form_of(body):
    return {key: values[0] for key, values in parse_qs(body.decode(), max_num_fields=10).items()}


async def accounts_page(request: Request):
    """The accounts (GET), and an admin's action on one (POST: add, role, close, link). A new link is shown once,
    on the page answering the action: it is not kept anywhere it could be read again."""
    done, error, link = None, None, None
    if request.method == "POST":
        form = form_of(await request.body())
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
    return templates.TemplateResponse(request, "accounts.html", context, status_code=400 if error else 200)


async def invitation_page(request: Request):
    """Choosing one's password through a one-time link (GET the form, POST the password twice), then to the login."""
    token = request.path_params["token"]
    found = await accounts.invited(token)
    if found is None:
        return templates.TemplateResponse(request, "invitation.html", {"gone": True}, status_code=404)
    error = None
    if request.method == "POST":
        from app.server import same_origin  # the server imports these pages

        if not same_origin(request):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        form = form_of(await request.body())
        if form.get("password", "") != form.get("again", ""):
            error = "les deux mots de passe diffèrent"
        else:
            try:
                email = await accounts.accept(token, form.get("password", ""))
                await audit.record(email, "password_set", email, via="lien")
                return RedirectResponse("/login?ready=1", status_code=303)
            except ValueError as refused:
                error = str(refused)
    context = {"found": found, "error": error, "min": accounts.MIN_PASSWORD}
    return templates.TemplateResponse(request, "invitation.html", context, status_code=400 if error else 200)
