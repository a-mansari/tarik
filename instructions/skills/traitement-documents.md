---
name: Traitement des documents
description: Traiter les factures et relevés bancaires d'un client pour un dossier, c'est-à-dire les saisir, les enregistrer ou les comptabiliser dans Experio. À charger dès qu'un client envoie, joint ou mentionne des factures ou des relevés à traiter, saisir, enregistrer ou comptabiliser.
---
DÉCIDE TOUT DE SUITE :
- Le client nomme un dossier, dans ce message ou plus haut dans la conversation → appelle Experio_traiter_fichiers avec ce nom, tel qu'il l'a écrit. Ne cherche pas le dossier toi-même : l'outil le retrouve, même mal orthographié.
- Une facture est soit un achat (le client l'a reçue d'un fournisseur), soit une vente (le client l'a émise pour son propre client). Le client a écrit « achat », « fournisseur », « vente » ou « factures clients » → invoice_type = achat ou vente ; les deux → mixte.
- Le client envoie des factures sans dire si ce sont des achats ou des ventes → demande-le-lui avant tout, dans le même message que le dossier s'il manque aussi. N'appelle pas Experio_traiter_fichiers. Pour des relevés bancaires seuls, ne demande rien : invoice_type vide.
- Le client ne nomme aucun dossier, ni dans ce message ni plus haut → demande-lui pour quel dossier. N'appelle pas Experio_traiter_fichiers.
- Tu ne vois aucun fichier dans le message → appelle quand même Experio_traiter_fichiers : c'est lui qui trouve les fichiers joints.

ÉTAPES :
1. Experio_traiter_fichiers, une seule fois, avec dossier = le nom du dossier tel que le client l'a écrit, invoice_type = ce que le client a écrit, et les consignes qu'il a écrites lui-même avec ses factures (elles valent pour toutes les factures du message) : tva_non_recuperable (« oui » / « non »), retenue (ses mots recopiés), repartir (« oui » / « non »), compte (le compte qu'il nomme, recopié), mode_paiement, exercice (« nouveau » ou l'exercice ouvert qu'il nomme). Chacune vide quand le client n'en dit rien : vide veut dire « comme d'habitude », jamais une supposition.
2. Recopie sa réponse mot pour mot, facture par facture, sans la reformuler. Garde les numéros exacts.

QUAND Experio_traiter_fichiers RÉPOND :
- Il demande de quel dossier il s'agit (aucun dossier trouvé, ou plusieurs proches) → pose la question au client telle quelle, sans rien traiter d'autre. Quand le client donne le nom, rappelle Experio_traiter_fichiers avec ce nom et le même invoice_type.

NE JAMAIS :
- dire qu'aucun fichier n'est joint sans avoir appelé Experio_traiter_fichiers ;
- appeler Experio_traiter_fichiers deux fois pour la même demande, sauf pour donner le dossier qu'il a demandé ;
- deviner invoice_type à la place du client, même si les fichiers semblent clairs ;
- remplir une consigne (tva_non_recuperable, retenue, repartir, compte, mode_paiement, exercice) que le client n'a pas écrite, ou la déduire des factures ;
- demander une confirmation avant de traiter ;
- traiter un FEC ou un grand livre ici : c'est la compétence « Nouveau dossier ».
