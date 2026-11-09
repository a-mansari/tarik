"""Serve a new dossier's form (engine/tools/dossiers/form.py), open without a login: the one-time token in its address
is its key, and it names the cabinet, so a form only ever reaches its own cabinet's dossier.

The page is plain HTML, every question on it, and works so in any browser a client opens an email's link with. A small
script (allowed by a nonce, made for each page) turns it into a wizard, one question at a time with a progress bar and
a recap before sending; without it the same form is one page with one button. No font, image or script from elsewhere:
the token is in the address, and the page belongs to the client."""

import secrets
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import HTMLResponse

from engine.calls import limited, within
from engine.tools.core.app import ToolError
from engine.tools.dossiers.form import fill_form, form_spec, open_form
from engine.tools.dossiers.guide import as_list, first

MOST = 100_000  # bytes of a posted form (twelve notes of 500 accented letters are about 36 000), never read past
FIELDS = 80  # fields of a posted form: about 45 at most
TOO_LONG = "Le formulaire envoyé est trop long : raccourcissez vos précisions."

STYLE = (Path(__file__).parent / "form.css").read_text()


SCRIPT = (Path(__file__).parent / "form.js").read_text()


def shown(title, body, status=200, script=False):
    """Return an HTML page titled ``title`` holding ``body`` (already safe HTML); with ``script``, the wizard's script,
    allowed by a nonce made for this page alone."""
    nonce = secrets.token_urlsafe(16)  # allows this page's one style and one script, nothing else
    tag = f'<script nonce="{nonce}">{SCRIPT}</script>' if script else ""
    html = (
        '<!doctype html><html lang="fr"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        f'<title>{escape(title)}</title><style nonce="{nonce}">{STYLE}</style><body><div class="wrap">'
    )
    headers = {
        "cache-control": "no-store",
        "referrer-policy": "no-referrer",  # the token is in the address
        "content-security-policy": f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    }
    return HTMLResponse(f"{html}{body}</div>{tag}", status, headers)


def option(control, item, chosen, posted):
    """Return the HTML of one option row of ``control``, ticked when its key is among ``chosen``."""
    kind = "checkbox" if control["kind"] == "many" else "radio"
    ticked = " checked" if item["key"] in chosen else ""
    code = f" <code>{escape(item['code'])}</code>" if item.get("code") else ""
    desc = f"<small>{escape(item['desc'])}</small>" if item.get("desc") else ""
    other = ""
    if item.get("other"):
        text = [first(posted, f"{control['key']}__other") if posted else control.get("other", "")]
        other = (
            f'<input class="other-in" name="{escape(control["key"])}__other" value="{escape(text[0])}" '
            f'type="{"number" if item.get("num") else "text"}" placeholder="{escape(item["other"])}" '
            f'aria-label="{escape(item["other"])}" maxlength="200">'
        )
    return (
        f'<label class="opt"><input type="{kind}" name="{escape(control["key"])}" '
        f'value="{escape(item["key"])}"{ticked}>'
        f'<span class="mark"></span><span class="txt"><b>{escape(item["label"])}{code}</b>{desc}{other}</span></label>'
    )


def control_html(control, posted):
    """Return the HTML of one control of form_spec's (a card), filled as ``posted`` when the form was posted (else as
    the spec says)."""
    key, label = control["key"], escape(control["label"])
    kind = control["kind"]
    if kind == "fixed":
        shown_value = escape(control["value"])
        return f'<div class="card"><label class="field">{label}<input value="{shown_value}" disabled></label></div>'
    if kind == "text":
        value = first(posted, key) if posted else control["value"]
        return (
            f'<div class="card"><label class="field">{label}'
            f'<input type="text" name="{escape(key)}" value="{escape(value)}" inputmode="numeric" autocomplete="off" '
            f'maxlength="40"></label></div>'
        )
    chosen = control["selected"]
    if posted:  # "" is a choice too: the radio that changes nothing
        chosen = [posted[key]] if isinstance(posted.get(key), str) else posted.get(key, [])
    why = f'<p class="why">{escape(control["why"])}</p>' if control.get("why") else ""
    rows = "".join(option(control, item, chosen, posted) for item in control["options"])
    note = ""
    if control.get("note") is not None:
        text = first(posted, f"{key}__note") if posted else control["note"]
        note = (
            f"<details{' open' if text else ''}><summary>Ajouter une précision</summary>"
            f'<textarea name="{escape(key)}__note" maxlength="500" '
            'placeholder="Une règle particulière à ce dossier, '
            f'un cas à connaître">{escape(text)}</textarea></details>'
        )
    seen = (
        f'<input type="hidden" name="{escape(key)}__seen" value="1">' if kind == "many" else ""
    )  # none ticked = cleared
    return f'<div class="card"><h3>{label}</h3>{why}<div class="opts {kind}">{rows}</div>{seen}{note}</div>'


def step_html(step, posted):
    """Return the HTML of one step of form_spec's: a fieldset, its section's tag and title, its cards."""
    tag = f'<span class="tag">{escape(step["tag"])}</span>' if step.get("tag") else ""
    hint = f'<p class="hint">{escape(step["hint"])}</p>' if step.get("hint") else ""
    cards = "".join(control_html(control, posted) for control in step["controls"])
    title = escape(step["title"])
    return f'<fieldset class="step"><legend class="sec">{tag}<span>{title}</span></legend>{hint}{cards}</fieldset>'


def posted_of(text):
    """Return a posted body as {name: text}, or {name: [texts]} for a name posted more than once (a question with
    several options ticked). Raises ToolError for more than FIELDS fields."""
    found = {}
    try:
        pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=FIELDS)  # a blank radio is a choice
    except ValueError:
        raise ToolError(TOO_LONG) from None
    for name, value in pairs:
        found[name] = [*as_list(found[name]), value] if name in found else value
    return found


async def body_of(request):
    """Return the posted body as text, at most MOST bytes of it read. Raises ToolError when it is longer: never a form
    taken as empty, which would burn the link and say thanks."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MOST:
            raise ToolError(TOO_LONG)
    return raw.decode("utf-8", "replace")


async def form_page(request: Request):
    """GET or POST /formulaire/{cabinet}/{token}: show the form of a new dossier, or apply what was posted.

    The link is checked first, outside the cabinet's call slots (an open route must not let a guessed link hold them);
    only then Experio is reached. POST takes the form's own fields (urlencoded) and shows what was set; a refusal (a
    value wrong, a body too long, an ICE or IF another dossier has, Experio's check) shows the form again, filled as
    posted, with the reason. 404 when the cabinet is unknown or the link is unknown, expired or already used."""
    cabinet, token = request.path_params["cabinet"], request.path_params["token"]
    error, posted = None, {}
    try:
        await within(cabinet, open_form, token)
        if request.method == "POST":
            try:
                posted = posted_of(await body_of(request))
                done = await limited(cabinet, fill_form, token, posted)
                items = "".join(f"<li>{escape(line)}</li>" for line in done)
                said = "<p>Je vous ai aussi écrit pour confirmer, dans votre conversation avec Tarik.</p>"
                return shown(
                    "Merci, c'est enregistré",
                    f"<header><h1>Merci</h1></header><p>Vos choix pour ce dossier :</p><ul>{items}</ul>"
                    f"{said if done.confirmed else ''}",
                )
            except ToolError as refused:
                error = str(refused)
        spec = await limited(cabinet, form_spec, token)
    except ToolError, PermissionError:
        return shown(
            "Formulaire indisponible",
            "<header><h1>Formulaire indisponible</h1></header><p>Ce lien n'existe pas, a expiré ou a déjà servi. "
            "Écrivez à Tarik pour en recevoir un nouveau.</p>",
            404,
        )
    problem = f'<p class="bad" role="alert">{escape(error)}</p>' if error else ""
    steps = "".join(step_html(step, posted) for step in spec["steps"])
    head = (
        f'<header><div class="kicker">Experio · Tarik</div><h1>Dossier {escape(spec["dossier"])}</h1>'
        "<p class=\"lead\">Répondez à ces questions pour ce dossier. Le type, l'ICE, l'IF et la TVA sont écrits tout "
        "de suite dans Experio. Vos autres choix sont enregistrés pour le dossier : Tarik les suivra à mesure qu'il "
        "sait le faire. Toutes les questions sont facultatives, "
        "et une question laissée vide ne change rien.</p></header>"
    )
    bar = (
        '<div class="progress" aria-live="polite"><div class="bar"><i id="fill"></i></div>'
        '<div class="count" id="count"></div></div>'
    )
    nav = (
        '<div class="btns"><button type="button" class="ghost js-only" id="prev">← Précédent</button>'
        '<button type="button" class="js-only js-next" id="next">Suivant →</button>'
        '<button type="submit" id="send">Enregistrer</button></div>'
    )
    recap = '<section id="recap"><h2>Récapitulatif</h2><div id="list"></div></section>'
    form = f'<form method="post" id="form" novalidate{" data-error" if error else ""}>{steps}{recap}{nav}</form>'
    return shown(f"Dossier {spec['dossier']}", f"{head}{problem}{bar}{form}", 400 if error else 200, script=True)
