---
name: Traitement des documents
description: Traiter les factures et relevés bancaires d'un client pour un dossier, c'est-à-dire les saisir, les enregistrer ou les comptabiliser dans Experio. À charger dès qu'un client envoie, joint ou mentionne des factures ou des relevés à traiter, saisir, enregistrer ou comptabiliser.
---
DÉCIDE TOUT DE SUITE :
- Le client nomme un dossier, dans ce message ou plus haut dans la conversation → appelle Experio_traiter_fichiers avec ce nom, tel qu'il l'a écrit. Ne cherche pas le dossier toi-même : l'outil le retrouve, même mal orthographié.
- Une facture est soit un achat (le client l'a reçue d'un fournisseur), soit une vente (le client l'a émise pour son propre client). Le client a écrit « achat », « fournisseur », « vente » ou « factures clients » → invoice_type = achat ou vente ; les deux → mixte.
- Le client n'a écrit ni dossier ni achat / vente → appelle quand même Experio_traiter_fichiers, avec dossier et invoice_type vides : c'est l'outil qui pose la question au client, jamais toi. Pour des relevés bancaires, invoice_type reste vide de toute façon.
- Le client demande de comptabiliser des factures qu'il a envoyées avant (« comptabilisez-les », « vous pouvez les passer en compta ») → appelle Experio_traiter_fichiers avec comptabiliser = « oui » : c'est lui qui retrouve ces factures par le dossier. Sans nom de dossier, laisse dossier vide : l'outil le demande, et à sa réponse rappelle avec le dossier ET comptabiliser = « oui ».
- Le client demande de reprendre, relancer ou finir des factures restées bloquées (« c'est réglé, reprenez », « relancez le traitement ») → appelle Experio_traiter_fichiers avec le dossier : il reprend les factures de ce dossier qu'une étape avait arrêtées, même d'une autre conversation. Sans nom de dossier, laisse dossier vide : l'outil le demande.
- Tu ne vois aucun fichier dans le message → appelle quand même Experio_traiter_fichiers : c'est lui qui trouve les fichiers joints.

ÉTAPES :
1. Experio_traiter_fichiers, une seule fois, avec dossier = le nom du dossier tel que le client l'a écrit, invoice_type = ce que le client a écrit, et les consignes qu'il a écrites lui-même avec ses factures (elles valent pour toutes les factures du message) : tva_non_recuperable (« oui » / « non »), retenue (ses mots recopiés), repartir (« oui » / « non »), compte (le compte qu'il nomme, recopié), mode_paiement, exercice (« nouveau » ou l'exercice ouvert qu'il nomme), comptabiliser (« oui » seulement quand il demande lui-même de comptabiliser : ses factures de ce message, ou des factures laissées en brouillon, même si elles n'étaient pas dans ce message ni cette conversation : il les a envoyées avant. Sinon vide : les factures sont importées et vérifiées, laissées en brouillon). Chacune vide quand le client n'en dit rien : vide veut dire « comme d'habitude », jamais une supposition.
2. Recopie sa réponse mot pour mot, facture par facture, sans la reformuler. Garde les numéros exacts.

QUAND Experio_traiter_fichiers RÉPOND :
- Il demande de quel dossier il s'agit (aucun nom, aucun dossier trouvé, ou plusieurs proches) → pose la question au client telle quelle, sans rien traiter d'autre. Quand le client donne le nom, rappelle Experio_traiter_fichiers avec ce nom, le même invoice_type et les mêmes consignes (comptabiliser compris).
- Il liste des lignes « QUESTION : » (des questions restées sans réponse sur les factures du dossier, même posées dans une autre conversation) → pose-les au client telles quelles. Sa réponse se transmet avec Experio_repondre.
- Il demande si ce sont des factures d'achat ou de vente (même sur une ligne « QUESTION : ») → pose la question au client telle quelle. Quand le client répond, rappelle Experio_traiter_fichiers avec le même dossier et invoice_type = sa réponse, comme pour le dossier.

NE JAMAIS :
- dire qu'aucun fichier n'est joint sans avoir appelé Experio_traiter_fichiers ;
- appeler Experio_traiter_fichiers deux fois pour la même demande, sauf pour donner le dossier qu'il a demandé ;
- deviner invoice_type ou le dossier à la place du client, même si les fichiers semblent clairs : vide veut dire « demande-le-lui », l'outil s'en charge ;
- remplir une consigne (tva_non_recuperable, retenue, repartir, compte, mode_paiement, exercice, comptabiliser) que le client n'a pas écrite, ou la déduire des factures ;
- demander une confirmation avant de traiter ;
- traiter un FEC ou un grand livre ici : c'est la compétence « Nouveau dossier ».
