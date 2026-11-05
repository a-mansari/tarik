"""Tarik's guide for a dossier: the 12 questions a manager answers on the dossier's form (form.py) to say how Tarik
should work on it: what it files where, how it treats the bank, what it does with a doubtful document, what it may do
alone. Each answer is kept for the dossier alone, in tarik_dossier_behaviour under the question's id: {"sel": [the
options chosen, by key], "other": what an "other" option was filled with, "note": a free precision}.

These are Tarik's own rules, so unlike a dossier's TVA (Experio's) they have a home here. Nothing acts on an answer yet
but the one on what Tarik may do alone: ticking "post_clear" is the dossier's post_invoices setting (behaviour.py), kept
by the form as that one flag. Each rule gets its behaviour when Tarik is made to follow it; until then the answers are
the dossier's recorded preferences, which the form says."""

from engine.tools.core.app import ToolError

NOTE_SIZE, OTHER_SIZE = 500, 200
KEY = "guide_"  # what a question's id is prefixed with in tarik_dossier_behaviour
POST_CLEAR = ("validate_alone", "post_clear")  # the question and the option that is post_invoices

SECTIONS = {
    "A": ("Règles de comptabilisation", "Où Tarik range les écritures de ce dossier."),
    "B": ("Banque et rapprochement", "Comment Tarik traite les relevés."),
    "C": ("Cas douteux", "Quand une pièce ou un montant paraît anormal."),
    "D": ("Autonomie et validation", "Les limites de ce que Tarik peut faire sans vous."),
}

# Each: id (the key it is kept under), section, name (short, for the recap), q, why, type ("one" or "many"), options
# [{key, label, code, desc, other (the placeholder of its free field), num (that field is a number), exclusive}].
QUESTIONS = [
    {
        "id": "sales_accounts",
        "section": "A",
        "name": "Comptes de vente",
        "q": "Pour les ventes de ce dossier, quel compte dois-je utiliser ?",
        "why": "Plusieurs réponses possibles si le dossier a plusieurs activités.",
        "type": "many",
        "options": [
            {
                "key": "goods_ma",
                "label": "Marchandises, Maroc",
                "code": "7111",
                "desc": "Revente en l'état, client au Maroc",
            },
            {
                "key": "goods_export",
                "label": "Marchandises, export",
                "code": "7113",
                "desc": "Revente en l'état, client à l'étranger",
            },
            {
                "key": "goods_made",
                "label": "Biens produits",
                "code": "7121",
                "desc": "Ventes de ce que l'entreprise fabrique",
            },
            {
                "key": "services",
                "label": "Services produits",
                "code": "7124",
                "desc": "Prestations, honoraires, travaux",
            },
            {"key": "other", "label": "Autre compte", "other": "Numéro et intitulé du compte"},
        ],
    },
    {
        "id": "mixed_invoice",
        "section": "A",
        "name": "Facture mixte",
        "q": "Si une facture mélange plusieurs activités, je la ventile ou je prends le compte principal ?",
        "type": "one",
        "options": [
            {
                "key": "split",
                "label": "Ventiler par activité",
                "desc": "Une ligne d'écriture par activité, sur son compte",
            },
            {"key": "main", "label": "Compte principal", "desc": "Tout sur le compte de l'activité dominante"},
            {"key": "ask", "label": "Me demander", "desc": "Tarik met la facture en attente et pose la question"},
        ],
    },
    {
        "id": "new_tiers",
        "section": "A",
        "name": "Tiers inconnu",
        "q": "Pour un fournisseur ou un client inconnu, je crée le tiers ou je vous demande ?",
        "why": "Un tiers créé reçoit un compte automatique : 4411 fournisseur, 3421 client.",
        "type": "one",
        "options": [
            {
                "key": "create",
                "label": "Créer automatiquement",
                "desc": "Tarik crée le tiers avec les données de la facture",
            },
            {
                "key": "create_if_ice",
                "label": "Créer si l'ICE est lisible",
                "desc": "Sinon la pièce attend votre réponse",
            },
            {"key": "ask", "label": "Toujours me demander", "desc": "Aucune création de tiers sans votre accord"},
        ],
    },
    {
        "id": "reconcile",
        "section": "B",
        "name": "Rapprochement",
        "q": "Je rapproche chaque ligne ou seulement les lignes sûres ?",
        "type": "one",
        "options": [
            {"key": "all", "label": "Chaque ligne", "desc": "Tarik rapproche tout ce qu'il peut"},
            {
                "key": "safe",
                "label": "Lignes sûres seulement",
                "desc": "Pièce trouvée et montant identique. Le reste vous est signalé",
            },
            {
                "key": "at_closing",
                "label": "Seulement à la clôture",
                "desc": "Pas de rapprochement en cours d'exercice",
            },
        ],
    },
    {
        "id": "no_document",
        "section": "B",
        "name": "Mouvement sans pièce",
        "q": "Que faire d'un mouvement bancaire sans pièce ?",
        "type": "one",
        "options": [
            {
                "key": "suspense",
                "label": "Compte d'attente",
                "desc": "Tarik l'enregistre en attente et vous prévient à la fin",
            },
            {
                "key": "report",
                "label": "Me le signaler tout de suite",
                "desc": "Rien n'est saisi tant que vous n'avez pas répondu",
            },
            {
                "key": "leave",
                "label": "Laisser non rapproché",
                "desc": "La ligne reste en l'état dans la liste des anomalies",
            },
        ],
    },
    {
        "id": "threshold",
        "section": "B",
        "name": "Seuil de demande",
        "q": "À partir de quel montant dois-je vous demander avant de saisir ?",
        "type": "one",
        "options": [
            {"key": "1000", "label": "1 000 MAD"},
            {"key": "5000", "label": "5 000 MAD"},
            {"key": "10000", "label": "10 000 MAD"},
            {"key": "50000", "label": "50 000 MAD"},
            {"key": "other", "label": "Autre seuil", "other": "Montant en MAD", "num": True},
        ],
    },
    {
        "id": "bad_document",
        "section": "C",
        "name": "Pièce douteuse",
        "q": "Pièce illisible, en double ou hors exercice : que fais-je ?",
        "type": "one",
        "options": [
            {"key": "reject", "label": "La rejeter", "desc": "Elle n'est pas comptabilisée, avec un motif noté"},
            {"key": "aside", "label": "La mettre de côté", "desc": "Elle reste dans une liste à traiter plus tard"},
            {"key": "escalate", "label": "Vous la remonter", "desc": "Vous décidez pièce par pièce"},
        ],
    },
    {
        "id": "unusual_amount",
        "section": "C",
        "name": "Montant inhabituel",
        "q": "Montant inhabituel par rapport à l'historique du client : j'alerte ou je saisis ?",
        "type": "one",
        "options": [
            {"key": "alert_first", "label": "Alerter avant de saisir", "desc": "La saisie attend votre accord"},
            {
                "key": "book_then_alert",
                "label": "Saisir puis alerter",
                "desc": "L'écriture est faite, la ligne est marquée à vérifier",
            },
            {
                "key": "book",
                "label": "Saisir sans alerte",
                "desc": "Seulement si le client a des montants très variables",
            },
        ],
    },
    {
        "id": "ambiguous_type",
        "section": "C",
        "name": "Type de pièce ambigu",
        "q": "Pièce dont le type est ambigu (achat, frais généraux, immobilisation) : je choisis ou je demande ?",
        "type": "one",
        "options": [
            {
                "key": "most_likely",
                "label": "Choisir le plus probable",
                "desc": "Tarik note son raisonnement dans l'écriture",
            },
            {"key": "hold", "label": "Mettre en attente", "desc": "Tarik propose un choix et attend votre validation"},
            {"key": "ask", "label": "Toujours demander", "desc": "Sans proposition préalable"},
        ],
    },
    {
        "id": "validate_alone",
        "section": "D",
        "name": "Validation seul",
        "q": "Qu'ai-je le droit de valider seul ?",
        "why": "Cochez tout ce que Tarik peut faire sans votre accord.",
        "type": "many",
        "options": [
            {"key": "draft", "label": "Passer en brouillon", "desc": "Enregistrer sans comptabiliser"},
            {
                "key": "post_clear",
                "label": "Comptabiliser les pièces claires",
                "desc": "Pièce lisible, tiers connu, compte évident",
            },
            {"key": "safe_matches", "label": "Valider les rapprochements sûrs", "desc": "Montant et pièce identiques"},
            {
                "key": "nothing",
                "label": "Rien sans validation",
                "desc": "Tout passe par vous avant comptabilisation",
                "exclusive": True,
            },
        ],
    },
    {
        "id": "modify_delete",
        "section": "D",
        "name": "Modifier ou supprimer",
        "q": "Qu'ai-je le droit de modifier ou de supprimer ?",
        "type": "one",
        "options": [
            {
                "key": "nothing",
                "label": "Rien sans accord",
                "desc": "Aucune modification ni suppression sans votre validation",
            },
            {
                "key": "own_entries",
                "label": "Modifier mes saisies, ne rien supprimer",
                "desc": "Tarik corrige ce qu'il a lui-même créé",
            },
            {
                "key": "own_drafts",
                "label": "Modifier et supprimer mes brouillons",
                "desc": "Les écritures comptabilisées restent intouchables",
            },
        ],
    },
    {
        "id": "report",
        "section": "D",
        "name": "Compte rendu",
        "q": "À quel moment dois-je vous envoyer un compte rendu ?",
        "type": "one",
        "options": [
            {"key": "each_anomaly", "label": "À chaque anomalie", "desc": "Un message dès qu'un cas bloque"},
            {"key": "end_session", "label": "En fin de session", "desc": "Un seul récapitulatif à la fin"},
            {
                "key": "end_and_blocking",
                "label": "Fin de session et anomalies bloquantes",
                "desc": "Récapitulatif, plus alerte immédiate pour ce qui bloque",
            },
            {"key": "daily", "label": "Chaque jour", "desc": "Un point quotidien tant que le dossier est ouvert"},
        ],
    },
]


def as_list(value):
    """Return a posted value as a list: [] for nothing, [value] for one text, the list as it is."""
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def option_label(option, other=""):
    """Return an option as the recap says it: its label, its account code, what an "other" option was filled with."""
    code = f" ({option['code']})" if option.get("code") else ""
    more = f" : {other or 'non précisé'}" if option.get("other") else ""
    return f"{option['label']}{code}{more}"


def read(answers, stored):
    """Return (kept, lines) for the guide's part of the form's ``answers`` ({question id: option key(s), "<id>__other":
    text, "<id>__note": text}) over what ``stored`` ({id: value}) holds: the answers to keep ({id: value}: those that
    differ from what is stored), and what was chosen, one line per answered question. A question left blank is not
    kept. Raises ToolError, naming the question's number only, for an option it lacks, two options of a single
    question, "nothing" with another option, or an "other" option left empty (or not a number where it is one)."""
    kept, lines = {}, []
    for number, question in enumerate(QUESTIONS, 1):
        chosen = [key for key in as_list(answers.get(question["id"])) if key]
        if not chosen:
            continue
        options = {option["key"]: option for option in question["options"]}
        where = f"Question {number}"
        if any(key not in options for key in chosen) or (question["type"] == "one" and len(chosen) > 1):
            raise ToolError(f"{where} : réponse inconnue.")
        alone = next((options[key] for key in chosen if options[key].get("exclusive")), None)
        if alone and len(chosen) > 1:
            raise ToolError(f"{where} : « {alone['label']} » ne va pas avec un autre choix.")
        other = str((as_list(answers.get(f"{question['id']}__other")) or [""])[0]).strip()[:OTHER_SIZE]
        wants_other = [options[key] for key in chosen if options[key].get("other")]
        if wants_other and not other:
            raise ToolError(f"{where} : précisez votre réponse.")
        if wants_other and wants_other[0].get("num") and not other.isdigit():
            raise ToolError(f"{where} : un montant en chiffres seulement.")
        note = str((as_list(answers.get(f"{question['id']}__note")) or [""])[0]).strip()[:NOTE_SIZE]
        value = {"sel": chosen, "other": other if wants_other else "", "note": note}
        if stored.get(question["id"]) != value:
            kept[question["id"]] = value
        said = " ; ".join(option_label(options[key], other) for key in chosen)
        lines.append(f"{question['name']} : {said}" + (f" ({note})" if note else ""))
    return kept, lines
