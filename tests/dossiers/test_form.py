"""A new dossier's form: its one-time link, what filling it writes (Experio's fields, Tarik's own per dossier), what it
refuses, and the open page that serves it."""

import pytest
from starlette.testclient import TestClient

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import behaviour, form, guide, identity
from engine.tools.dossiers.form_link import form_link
from engine.tools.invoices import pipeline

URL = "https://tarik.example"
declarations = []  # the dossier's TVA declarations in the fake Experio
HAVE = {"folder_type": "company", "ice": "", "vat": "", "declaration_mode": "quarterly", "regime_type": "encaissement"}


@pytest.fixture
def experio(db_cabinet, monkeypatch):
    """Experio holds dossier 7 (HAVE) and no other; its writes are collected: (model, method, ids, values)."""
    writes = []
    monkeypatch.setenv("PUBLIC_URL", URL)
    monkeypatch.setattr(form, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(form, "odoo_call", lambda model, method, ids, **kw: [HAVE])
    monkeypatch.setattr(form, "dry_run", lambda: False)
    monkeypatch.setattr(
        form, "odoo_json", lambda model, method, ids, values: writes.append((model, method, ids, values))
    )
    declarations.clear()  # no TVA declaration yet; a test adds one
    monkeypatch.setattr(  # changes_to reads the dossier, frequency_closed counts its declarations
        identity,
        "odoo_call",
        lambda model, method, ids, **kw: len(declarations) if method == "search_count" else [HAVE],
    )
    monkeypatch.setattr(form, "conflict", lambda dossier_id, name, changes: None)
    monkeypatch.setattr(form, "sender_of", lambda session_id: "client@societe.ma")
    return writes


def controls(spec):
    """The form's controls by key."""
    return {control["key"]: control for step in spec["steps"] for control in step["controls"]}


def token_of(link):
    return link.rsplit("/", 1)[1]


@pytest.mark.functionality
def test_without_the_public_address_no_form_is_made_and_tarik_asks_by_email(db_cabinet, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert form_link(7, "s") == ""
    with cabinet_db() as db:
        assert db.execute("select count(*) from tarik_dossier_forms").fetchone()[0] == 0


@pytest.mark.functionality
def test_a_dossier_s_link_names_its_cabinet_and_a_token_that_opens_its_form(experio, db_cabinet, monkeypatch):
    link = form_link(7, "s")
    assert link.startswith(f"{URL}/formulaire/{db_cabinet}/") and len(token_of(link)) >= 30
    spec = form.form_spec(token_of(link))
    assert spec["dossier"] == "ALPHA"
    assert [step["title"] for step in spec["steps"]][:2] == ["Le dossier", "TVA"]
    assert [c["key"] for c in spec["steps"][0]["controls"]] == ["folder_type", "ice", "if_number"]
    assert len(spec["steps"]) == 2 + len(guide.QUESTIONS)  # Experio's two, then the twelve questions
    asked = controls(spec)
    mode = asked["declaration_mode"]
    assert mode["selected"] == ["quarterly"] and mode["options"][0] == {"key": "", "label": "inchangé"}  # Experio's
    assert asked["validate_alone"]["selected"] == [] and asked["sales_accounts"]["kind"] == "many"
    monkeypatch.setitem(HAVE, "declaration_mode", "out_of_field")  # not a choice the form offers: shown, not editable
    again = controls(form.form_spec(token_of(link)))["declaration_mode"]
    assert again["kind"] == "fixed" and again["value"] == "hors champ"


@pytest.mark.functionality
def test_a_filled_form_writes_experio_s_fields_and_keeps_tarik_s_own_for_the_dossier_only(experio):
    token = token_of(form_link(7, "s"))
    lines = form.fill_form(
        token,
        {
            "folder_type": "company",
            "ice": "001234567000011",
            "declaration_mode": "monthly",
            "regime_type": "debit",
            "validate_alone": ["post_clear"],
        },
    )
    assert experio == [
        (
            "res.company",
            "write",
            [7],
            {
                "ice": "001234567000011",
                "declaration_mode": "monthly",
                "regime_type": "debit",
            },
        )
    ]
    assert "Déclaration de TVA : mensuelle" in lines and "Validation seul : Comptabiliser les pièces claires" in lines
    assert behaviour.dossier_behaviour(7, "post_invoices") is True
    assert behaviour.dossier_behaviour(8, "post_invoices") is False and behaviour.behaviour("post_invoices") is False
    with pytest.raises(ToolError, match="déjà été rempli"):  # one use
        form.fill_form(token, {"regime_type": "encaissement"})


@pytest.mark.security
@pytest.mark.parametrize(
    "answers",
    [
        {"regime_type": "n'importe quoi"},
        {"declaration_mode": "out_of_field"},
        {"ice": "12ab"},
        {"ice": "123"},
        {"folder_type": "banque"},
        {"validate_alone": ["nothing", "draft"]},
    ],
)
def test_a_wrong_answer_writes_nothing_and_leaves_the_form_open(experio, answers):
    token = token_of(form_link(7, "s"))
    with pytest.raises(ToolError):
        form.fill_form(token, {"declaration_mode": "monthly", **answers})
    assert experio == [] and form.open_form(token)[0] == 7


@pytest.mark.security
def test_an_ice_another_dossier_has_is_refused_and_nothing_is_written(experio, monkeypatch):
    token = token_of(form_link(7, "s"))
    monkeypatch.setattr(
        form, "conflict", lambda *args: {"status": "stop", "message": "le dossier `BETA` a déjà cet ICE"}
    )
    with pytest.raises(ToolError, match="autre dossier") as refused:
        form.fill_form(token, {"ice": "001234567000011", "validate_alone": ["post_clear"]})
    assert "BETA" not in str(refused.value)  # the page is open: no other dossier's name
    assert experio == [] and behaviour.dossier_behaviour(7, "post_invoices") is False


@pytest.mark.functionality
def test_only_what_differs_from_experio_is_written_and_a_real_ice_is_left_alone(experio, monkeypatch):
    monkeypatch.setitem(HAVE, "ice", "001234567000011")
    token = token_of(form_link(7, "s"))
    assert controls(form.form_spec(token))["ice"]["kind"] == "fixed"
    form.fill_form(token, {"ice": "009999999000099", "regime_type": "encaissement", "declaration_mode": ""})
    assert experio == []  # same regime, mode left, the ICE kept


@pytest.mark.security
def test_a_form_is_taken_once_and_given_back_when_experio_refuses(experio, monkeypatch):
    token = token_of(form_link(7, "s"))

    def refuse(*args):
        raise RuntimeError("secret internal reason")

    monkeypatch.setattr(form, "odoo_json", refuse)
    with pytest.raises(ToolError, match="Experio a refusé") as refused:
        form.fill_form(token, {"regime_type": "debit"})
    assert "secret" not in str(refused.value) and form.open_form(token)  # nothing leaked, open again
    with cabinet_db() as db:
        db.execute("update tarik_dossier_forms set filled_at = now()")  # taken by another post meanwhile
    with pytest.raises(ToolError, match="déjà été rempli"):
        form.fill_form(token, {"regime_type": "debit"})


@pytest.mark.security
def test_an_expired_form_is_closed(experio):
    token = token_of(form_link(7, "s"))
    with cabinet_db() as db:
        db.execute("update tarik_dossier_forms set expires_at = now() - interval '1 minute'")
    with pytest.raises(ToolError, match="expiré"):
        form.form_spec(token)


@pytest.mark.functionality
def test_in_test_mode_nothing_is_written_and_the_form_stays_open(experio, monkeypatch):
    monkeypatch.setattr(form, "dry_run", lambda: True)
    token = token_of(form_link(7, "s"))
    assert form.fill_form(token, {"validate_alone": ["post_clear"]})[0].endswith(
        "Validation seul : Comptabiliser les pièces claires"
    )
    assert experio == [] and behaviour.dossier_behaviour(7, "post_invoices") is False and form.open_form(token)


@pytest.mark.functionality
def test_a_new_dossier_is_told_with_its_form_link_when_there_is_one(experio, db_cabinet, monkeypatch):
    monkeypatch.setattr(identity, "dry_run", lambda: False)
    monkeypatch.setattr(identity, "lookalikes", lambda name, ice, if_number: ([], []))
    monkeypatch.setattr(identity, "odoo_json", lambda *args, **kw: 7)
    monkeypatch.setattr(identity, "main_dossier_context", lambda: {})
    monkeypatch.setattr(identity, "attach_user", lambda *args: None)
    made = identity.create_dossier(name="ALPHA", client_message="Créez le dossier ALPHA", session_id="s")
    assert made["dossier_id"] == 7 and f"{URL}/formulaire/{db_cabinet}/" in made["message"]
    assert "ne lui posez pas ces questions une à une" in made["message"]
    monkeypatch.delenv("PUBLIC_URL")
    other = identity.create_dossier(name="BETA", client_message="Créez le dossier BETA", session_id="t")
    assert "formulaire" not in other["message"].lower()


@pytest.mark.functionality
def test_a_dossier_set_to_post_posts_its_new_verified_invoices_unasked_and_the_client_can_still_hold_one(
    db_cabinet, monkeypatch
):
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"company_id": [7, "ALPHA"]})
    assert pipeline.hold_posting(1, "s")["status"] == "stop"  # nobody set it: held as before
    behaviour.set_for_dossier(7, "post_invoices", True, "client@societe.ma")
    assert pipeline.hold_posting(2, "s")["status"] == "ok"  # released at once
    assert pipeline.hold_posting(2, "s", "non")["status"] == "stop"  # the client takes it back...
    assert pipeline.hold_posting(2, "s")["status"] == "stop"  # ...and a later run keeps it held
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"company_id": [8, "BETA"]})
    assert pipeline.hold_posting(3, "s")["status"] == "stop"  # another dossier, not set


@pytest.mark.security
def test_the_open_page_serves_only_its_own_cabinet_s_open_form_and_escapes_what_it_shows(
    experio, db_cabinet, monkeypatch
):
    from server.server import app

    monkeypatch.setattr(form, "require_dossier", lambda dossier_id: "<script>alert(1)</script>")
    client = TestClient(app)  # no lifespan; the guard lets /formulaire/ through without a session
    token = token_of(form_link(7, "s"))
    shown = client.get(f"/formulaire/{db_cabinet}/{token}")
    assert shown.status_code == 200 and "<script>alert" not in shown.text and "&lt;script&gt;" in shown.text
    assert client.get(f"/formulaire/autre_cabinet/{token}").status_code == 404
    assert client.get(f"/formulaire/{db_cabinet}/devine").status_code == 404
    bad = client.post(
        f"/formulaire/{db_cabinet}/{token}", content="ice=12ab&validate_alone=draft&validate_alone=post_clear"
    )
    assert bad.status_code == 400 and "chiffres" in bad.text and 'value="12ab"' in bad.text
    done = client.post(
        f"/formulaire/{db_cabinet}/{token}", content="declaration_mode=monthly&validate_alone=post_clear"
    )
    assert done.status_code == 200 and "Merci" in done.text
    assert client.get(f"/formulaire/{db_cabinet}/{token}").status_code == 404  # used


@pytest.mark.functionality
def test_a_setting_left_as_shown_is_not_kept_so_the_dossier_goes_on_following_the_cabinet(experio):
    token = token_of(form_link(7, "s"))
    form.fill_form(token, {"validate_alone": ["draft"], "regime_type": "encaissement"})  # as the form showed them
    with cabinet_db() as db:
        assert db.execute("select count(*) from tarik_dossier_behaviour where key = 'post_invoices'").fetchone()[0] == 0
    behaviour.change("post_invoices", "oui", "chef@cabinet.ma")  # the cabinet turns it on later
    assert behaviour.dossier_behaviour(7, "post_invoices") is True
    other = token_of(form_link(8, "s"))
    form.fill_form(other, {"validate_alone": ["draft"]})  # a client who really chooses against the cabinet's value
    assert behaviour.dossier_behaviour(8, "post_invoices") is False


@pytest.mark.functionality
@pytest.mark.parametrize(("own", "picked", "kept"), [(True, "non", False), (False, "non", False), (True, "oui", True)])
def test_a_dossier_with_its_own_value_keeps_it_unless_the_client_picks_another(experio, own, picked, kept):
    behaviour.set_for_dossier(7, "post_invoices", own, "client@societe.ma")
    form.fill_form(token_of(form_link(7, "s")), {"validate_alone": ["post_clear"] if picked == "oui" else ["draft"]})
    assert behaviour.dossier_behaviour(7, "post_invoices") is kept


@pytest.mark.functionality
def test_a_frequency_closed_in_experio_is_shown_as_it_is_and_refused_on_submit(experio, monkeypatch):
    declarations.append(1)  # a TVA declaration exists: changing the frequency would falsify it
    token = token_of(form_link(7, "s"))
    mode = controls(form.form_spec(token))["declaration_mode"]
    assert mode["kind"] == "fixed" and mode["value"] == "trimestrielle"
    with pytest.raises(ToolError, match="Experio, pas ici"):
        form.fill_form(token, {"declaration_mode": "monthly", "regime_type": "debit"})
    assert experio == [] and form.open_form(token)  # nothing written, still open
    monkeypatch.setitem(HAVE, "declaration_mode", "out_of_field")
    shown = controls(form.form_spec(token))["declaration_mode"]
    assert shown["kind"] == "fixed" and shown["value"] == "hors champ"


def stored(dossier_id=7):
    with cabinet_db() as db:
        return dict(db.execute("select key, value from tarik_dossier_behaviour where dossier_id = %s", (dossier_id,)))


@pytest.mark.functionality
def test_the_guide_s_answers_are_kept_for_the_dossier_with_their_precision_and_shown_again(experio):
    token = token_of(form_link(7, "s"))
    lines = form.fill_form(
        token,
        {
            "sales_accounts": ["goods_ma", "other"],
            "sales_accounts__other": "7125 Ventes diverses",
            "sales_accounts__note": "Deux activités",
            "threshold": "other",
            "threshold__other": "25000",
            "reconcile": "safe",
            "report": "",  # left blank: not kept
        },
    )
    assert stored()["guide_sales_accounts"] == {
        "sel": ["goods_ma", "other"],
        "other": "7125 Ventes diverses",
        "note": "Deux activités",
    }
    assert stored()["guide_threshold"] == {"sel": ["other"], "other": "25000", "note": ""}
    assert stored()["guide_reconcile"]["sel"] == ["safe"] and "guide_report" not in stored()
    assert (
        "Comptes de vente : Marchandises, Maroc (7111) ; Autre compte : 7125 Ventes diverses (Deux activités)" in lines
    )
    assert "Seuil de demande : Autre seuil : 25000" in lines
    other = token_of(form_link(7, "s"))  # a new link for the same dossier: what it kept comes back
    shown = controls(form.form_spec(other))
    assert (
        shown["sales_accounts"]["selected"] == ["goods_ma", "other"]
        and shown["sales_accounts"]["note"] == "Deux activités"
    )
    assert shown["threshold"]["other"] == "25000" and shown["report"]["selected"] == []


@pytest.mark.functionality
def test_what_may_be_validated_alone_is_the_dossier_s_post_invoices_and_nothing_else_is_asked_twice(experio):
    form.fill_form(token_of(form_link(7, "s")), {"validate_alone": ["draft", "post_clear"]})
    assert behaviour.dossier_behaviour(7, "post_invoices") is True
    assert controls(form.form_spec(token_of(form_link(7, "s"))))["validate_alone"]["selected"] == [
        "draft",
        "post_clear",
    ]
    form.fill_form(token_of(form_link(7, "s")), {"validate_alone": ["draft"]})  # taken back
    assert behaviour.dossier_behaviour(7, "post_invoices") is False
    form.fill_form(token_of(form_link(7, "s")), {"reconcile": "all"})  # question not answered: post_invoices unchanged
    assert behaviour.dossier_behaviour(7, "post_invoices") is False


@pytest.mark.security
@pytest.mark.parametrize(
    "answers",
    [
        {"reconcile": "n'importe quoi"},
        {"mixed_invoice": ["split", "main"]},
        {"validate_alone": ["nothing", "draft"]},
        {"sales_accounts": ["other"]},
        {"threshold": "other", "threshold__other": "beaucoup"},
        {"sales_accounts": ["goods_ma", "exploit"]},
    ],
)
def test_a_wrong_guide_answer_says_which_question_without_echoing_it_and_writes_nothing(experio, answers):
    token = token_of(form_link(7, "s"))
    with pytest.raises(ToolError, match=r"Question \d+") as refused:
        form.fill_form(token, {"regime_type": "debit", **answers})
    assert "exploit" not in str(refused.value) and "n'importe" not in str(refused.value)
    assert experio == [] and stored() == {} and form.open_form(token)


@pytest.mark.functionality
def test_the_page_shows_every_question_without_a_script_and_a_wizard_script_allowed_by_its_nonce(experio, db_cabinet):
    from server.server import app

    client = TestClient(app)
    token = token_of(form_link(7, "s"))
    page = client.get(f"/formulaire/{db_cabinet}/{token}")
    assert page.status_code == 200 and page.text.count('<fieldset class="step">') == 2 + len(guide.QUESTIONS)
    assert '<button type="submit" id="send">' in page.text  # works with no script: one page, one button
    nonce = page.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page.text and "script-src 'nonce-" in page.headers["content-security-policy"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "http://" not in page.text and "https://" not in page.text  # no font, image or script from elsewhere
    assert (
        client.get(f"/formulaire/{db_cabinet}/{token}").headers["content-security-policy"]
        != page.headers["content-security-policy"]
    )


@pytest.mark.security
def test_a_refused_page_comes_back_filled_as_posted_and_escapes_the_client_s_words(experio, db_cabinet):
    from server.server import app

    client = TestClient(app)
    token = token_of(form_link(7, "s"))
    body = "ice=12ab&sales_accounts=goods_ma&sales_accounts=other&sales_accounts__other=%3Cscript%3Ex%3C%2Fscript%3E"
    body += "&reconcile__note=%22%3E%3Cimg+src%3Dx%3E&validate_alone=draft"
    back = client.post(f"/formulaire/{db_cabinet}/{token}", content=body)
    assert back.status_code == 400 and "chiffres" in back.text
    assert "<script>x" not in back.text and "&lt;script&gt;x" in back.text and "<img src=x>" not in back.text
    assert back.text.count(" checked>") == 3  # the two sales accounts and the "draft" option, as posted
    assert 'value="12ab"' in back.text


@pytest.mark.functionality
def test_a_question_of_several_choices_posted_with_none_ticked_is_cleared_and_a_precision_alone_is_kept(experio):
    form.fill_form(token_of(form_link(7, "s")), {"validate_alone": ["draft", "post_clear"], "report": "daily"})
    assert behaviour.dossier_behaviour(7, "post_invoices") is True
    lines = form.fill_form(token_of(form_link(7, "s")), {"validate_alone__seen": "1", "report__note": "Le lundi"})
    assert behaviour.dossier_behaviour(7, "post_invoices") is False  # nothing ticked on the page: no autonomous posting
    assert stored()["guide_validate_alone"] == {"sel": [], "other": "", "note": ""}
    assert stored()["guide_report"] == {"sel": [], "other": "", "note": "Le lundi"}  # a precision alone is kept
    assert "Validation seul : pas de réponse" in lines
    form.fill_form(token_of(form_link(7, "s")), {"reconcile": "all"})  # a question not on the page changes nothing
    assert stored()["guide_validate_alone"]["sel"] == []


@pytest.mark.technical
def test_a_duplicated_option_is_kept_once(experio):
    form.fill_form(token_of(form_link(7, "s")), {"validate_alone": ["draft", "draft", "post_clear"]})
    assert stored()["guide_validate_alone"]["sel"] == ["draft", "post_clear"]


@pytest.mark.security
def test_a_body_too_long_or_with_too_many_fields_is_refused_with_the_form_still_open_never_taken_as_empty(
    experio, db_cabinet
):
    from server.server import app

    client = TestClient(app)
    token = token_of(form_link(7, "s"))
    for body in ("reconcile__note=" + "é" * 60_000, "&".join(f"a{n}=1" for n in range(120))):
        refused = client.post(f"/formulaire/{db_cabinet}/{token}", content=body)
        assert refused.status_code == 400 and "trop long" in refused.text and "Merci" not in refused.text
        assert form.open_form(token)  # the link is not burned


@pytest.mark.security
def test_a_guessed_link_never_takes_one_of_the_cabinet_s_call_slots(experio, db_cabinet, monkeypatch):
    from server.api import form as page
    from server.server import app

    async def never(*args):
        raise AssertionError("a slot was taken")

    monkeypatch.setattr(page, "limited", never)
    client = TestClient(app)
    assert client.get(f"/formulaire/{db_cabinet}/devine").status_code == 404
    assert client.post(f"/formulaire/{db_cabinet}/devine", content="reconcile=all").status_code == 404
    assert client.get("/formulaire/autre_cabinet/devine").status_code == 404


@pytest.mark.functionality
def test_a_refused_page_ticks_the_radio_that_changes_nothing_again_and_opens_on_the_recap(experio, db_cabinet):
    from server.server import app

    client = TestClient(app)
    token = token_of(form_link(7, "s"))
    back = client.post(
        f"/formulaire/{db_cabinet}/{token}", content="declaration_mode=&threshold=other&threshold__other=x"
    )
    assert back.status_code == 400 and "Question 6" in back.text and "data-error" in back.text
    mode = back.text.split('name="declaration_mode" value=""')[1][:30]
    assert " checked" in mode  # "inchangé" is ticked again, not left empty
    assert "base-uri 'none'" in back.headers["content-security-policy"]
    assert "'unsafe-inline'" not in back.headers["content-security-policy"]
    nonce = back.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'<style nonce="{nonce}">' in back.text
