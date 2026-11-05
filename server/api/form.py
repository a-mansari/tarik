"""Serve a new dossier's form (engine/tools/dossiers/form.py), open without a login: the one-time token in its address
is its key, and it names the cabinet, so a form only ever reaches its own cabinet's dossier. Plain HTML, no script: it
works in any browser a client opens an email's link with."""

from html import escape
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import HTMLResponse

from engine.calls import limited
from engine.tools.core.app import ToolError
from engine.tools.dossiers.form import fill_form, form_spec

MOST = 20_000  # bytes of a posted form: a dozen short fields, never read past

STYLE = """body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1c1c1c}
label{display:block;margin-top:1rem;font-weight:600}small{display:block;font-weight:400;color:#555}
input,select{width:100%;padding:.5rem;font:inherit;margin-top:.25rem;box-sizing:border-box}
button{margin-top:1.5rem;padding:.6rem 1.2rem;font:inherit}.bad{background:#fde8e8;padding:.6rem 1rem;border-radius:6px}
li{margin:.25rem 0}"""


def shown(title, body, status=200):
    """Return an HTML page titled ``title`` holding ``body`` (already safe HTML)."""
    html = '<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    html += f'<meta name="robots" content="noindex"><title>{escape(title)}</title><style>{STYLE}</style>'
    headers = {
        "cache-control": "no-store",
        "referrer-policy": "no-referrer",  # the token is in the address
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'",
    }
    return HTMLResponse(f"{html}<h1>{escape(title)}</h1>{body}", status, headers)


def field(item, value):
    """Return the HTML of one form field of form_spec's, holding ``value``."""
    help_ = f"<small>{escape(item['help'])}</small>" if item.get("help") else ""
    name = escape(item["key"])
    if item["kind"] == "fixed":
        return f'<label>{escape(item["label"])}<input value="{escape(value)}" disabled></label>'
    if item["kind"] == "choice":
        options = "".join(
            f'<option value="{escape(key)}"{" selected" if key == value else ""}>{escape(label)}</option>'
            for key, label in item["options"]
        )
        control = f'<select name="{name}">{options}</select>'
    else:
        control = f'<input name="{name}" value="{escape(value)}" inputmode="numeric" autocomplete="off">'
    return f"<label>{escape(item['label'])}{help_}{control}</label>"


async def body_of(request):
    """Return the posted body as text, at most MOST bytes of it read: "" when it is longer."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MOST:
            return ""
    return raw.decode("utf-8", "replace")


async def form_page(request: Request):
    """GET or POST /formulaire/{cabinet}/{token}: show the form of a new dossier, or apply what was posted.

    POST takes the form's own fields (urlencoded) and shows what was set; a refusal (a value wrong, an ICE or IF
    another dossier has, Experio's check) shows the form again, filled as posted, with the reason. 404 when the cabinet
    is unknown or the link is unknown, expired or already used."""
    cabinet, token = request.path_params["cabinet"], request.path_params["token"]
    error, posted = None, {}
    try:
        if request.method == "POST":
            posted = dict(parse_qsl(await body_of(request)))
            try:
                done = await limited(cabinet, fill_form, token, posted)
                items = "".join(f"<li>{escape(line)}</li>" for line in done)
                return shown("Merci, c'est enregistré", f"<p>Réglages du dossier :</p><ul>{items}</ul>")
            except ToolError as refused:
                error = str(refused)
        spec = await limited(cabinet, form_spec, token)
    except ToolError, PermissionError:
        return shown(
            "Formulaire indisponible",
            "<p>Ce lien n'existe pas, a expiré ou a déjà servi. Écrivez à Tarik pour en recevoir un nouveau.</p>",
            404,
        )
    problem = f'<p class="bad" role="alert">{escape(error)}</p>' if error else ""
    fields = "".join(field(item, posted.get(item["key"], item["value"])) for item in spec["fields"])
    body = f'{problem}<form method="post">{fields}<button type="submit">Enregistrer</button></form>'
    return shown(f"Dossier {spec['dossier']}", body, 400 if error else 200)
