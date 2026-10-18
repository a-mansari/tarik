"""A realistic "Tarik - Traitement" run for the summary's tests: one PDF of ten invoices processed, each step's
outcome as its tool words it and marks it (routine or not), recorded as n8n records them (record_step)."""

# ruff: noqa: E501 -- its messages are the tools' own, one string each, as they reach the summary.

from unittest import mock

from tarik.invoices import pipeline
from tarik.invoices.pipeline import record_step
from tests.fakes import FakeDb

POSTED = (21, 22, 23, 24, 25)  # all the way to posting, nothing special


def through(invoice_id, tiers, account):
    """The outcomes of an invoice processed all the way, nothing to tell but its tiers, account and posting."""
    return [
        (
            "create_draft_invoice",
            "ok",
            f"factures.pdf : facture brouillon n° {invoice_id} creee dans le dossier GECAFISC.",
            True,
        ),
        ("wait_until_read", "ok", f"Facture {invoice_id} : lue par Experio.", True),
        ("check_invoice", "ok", f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan.", True),
        ("correct_invoice", "ok", f"Facture {invoice_id} : rien a corriger.", True),
        ("classify_invoice", "ok", f"Facture {invoice_id} : achat, deja enregistree comme telle (fournisseur).", True),
        (
            "link_tiers",
            "ok",
            f"Facture {invoice_id} : fournisseur {tiers}, Compte Tiers fournisseur 4411{invoice_id}.",
            False,
        ),
        ("check_duplicate", "ok", f"Facture {invoice_id} : pas de doublon dans le dossier.", True),
        ("assign_account", "ok", f"Facture {invoice_id} : compte {account} (compte memorise pour ce tiers).", False),
        ("settle_tva", "ok", f"Facture {invoice_id} : TVA recuperable. Lignes : 1000.0 HT a 20.0 % (TVA 20%).", True),
        ("check_journal", "ok", f"Facture {invoice_id} : journal Achats.", True),
        ("settle_withholding", "ok", f"Facture {invoice_id} : sans retenue a la source.", True),
        ("settle_prepaid", "ok", f"Facture {invoice_id} : pas de periode sur plusieurs mois, rien a repartir.", True),
        ("settle_exercice", "ok", f"Facture {invoice_id} : exercice Exercice 2026.", True),
        ("post_invoice", "ok", f"Facture {invoice_id} : comptabilisee.", False),
    ]


def outcomes():
    """{invoice id (or -file id): [(step, status, message, routine)]} for the whole run."""
    run = {-7: [("split_invoices", "ok", "factures.pdf : 10 factures : n° 21 a 30.", False)]}
    tiers = ["ATLAS TRANSPORT", "NOVA TELECOM", "MAROC BUREAU", "SOCIETE GENERALE DES EAUX", "ORBIT SERVICES"]
    for invoice_id, name in zip(POSTED, tiers, strict=True):
        run[invoice_id] = through(invoice_id, name, "6125 Achats de fournitures de bureau")
    asked = through(26, "", "")[:5] + [
        (
            "link_tiers",
            "question",
            "Facture 26 : fournisseur « ATLAS » introuvable par ICE/IF. Tiers proches : ATLAS DE ROULEMENT (id 7) 92 %; ATLAS TRANSPORT (id 8) 88 %. Lequel est-ce ? Sinon, donnez un autre nom ou l'ICE du tiers.",
            False,
        ),
    ]
    duplicate = through(27, "NOVA TELECOM", "")[:6] + [
        (
            "check_duplicate",
            "stop",
            "Facture 27 : DOUBLON, deja enregistree dans le dossier. Meme facture que le brouillon 22, saisi avant. Ne pas la traiter.",
            False,
        ),
    ]
    special = through(28, "SHELL MAROC", "6142 Voyages et deplacements")
    special[8] = ("settle_tva", "ok", "Facture 28 : TVA non recuperable. Lignes : 500.0 HT a 10.0 % (TVA 10%).", False)
    special[12] = (
        "settle_exercice",
        "ok",
        "Facture 28 : exercice 2025 cree et ouvert, la facture y est rattachee.",
        False,
    )
    withholding = through(29, "CABINET CONSEIL ALAMI", "6136 Honoraires")[:10] + [
        (
            "settle_withholding",
            "question",
            "Facture de CABINET CONSEIL ALAMI : retenue a la source sur ces honoraires ? 1. Sans retenue 2. Retenue de 10 % 3. Autre taux. Repondez 1, 2 ou 3.",
            False,
        ),
    ]
    failed = through(30, "", "")[:2] + [
        ("check_invoice", "error", "Verification de la facture : echec, reessayez plus tard.", False),
    ]
    run.update({26: asked, 27: duplicate, 28: special, 29: withholding, 30: failed})
    return run


def record(run_id, routine=True):
    """Record the run under ``run_id``, as n8n would (each invoice's steps in reverse: n8n records a step after the
    ones it led to); with ``routine`` False, as before outcomes were marked: every one shown."""
    for key, steps in outcomes().items():
        for step, status, message, unread in reversed(steps):
            ids = {"invoice_id": key} if key > 0 else {"file_id": -key}
            result = {"status": status, "message": message, **ids, **({"routine": True} if unread and routine else {})}
            record_step(run_id, "s", f"Etape ({step})", result)


def kept():
    """What the client must read whatever the summary keeps: each question, stop and problem, word for word."""
    return [message for steps in outcomes().values() for _, status, message, _ in steps if status != "ok"]


def summary(routine=True):
    """run_summary of the run without a database: its rows as record_step stores them (``routine``: as record)."""
    rows = [
        (key, step, status, message, unread and routine)
        for key, steps in outcomes().items()
        for step, status, message, unread in reversed(steps)
    ]
    db = FakeDb()
    db.fetchall = lambda: rows
    with mock.patch.object(pipeline, "cabinet_db", lambda: db):
        return pipeline.run_summary("r")["message"]
