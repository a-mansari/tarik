---
name: Réponse à une question
description: Le client répond à une question que Tarik a posée pendant le traitement de ses factures ou relevés (le tiers, le compte, la TVA, une retenue à la source, une charge constatée d'avance, l'exercice, le compte bancaire d'un relevé), ou demande de reprendre ce traitement. À charger dès que le message du client est une telle réponse.
---
DÉCIDE TOUT DE SUITE :
- Le message du client répond à une question que le traitement a posée plus haut (une ligne « QUESTION : » dans une réponse d'Experio_traiter_fichiers ou d'Experio_repondre) sur une facture ou un relevé (« le compte 6125 », « oui », « non recuperable », « à la charge de la société », « l'exercice 2025 », « le compte BMCE ») → appelle Experio_repondre.
- Le client demande de reprendre ou de finir le traitement de ses factures → appelle Experio_repondre.
- Le message est une nouvelle demande (de nouvelles factures, un autre dossier) → ce n'est pas une réponse : suis la compétence de cette demande.
- Le client répond à ta propre question (pour quel dossier, achat ou vente) → ce n'est pas pour Experio_repondre : continue « Traitement des documents » et appelle Experio_traiter_fichiers.
- Le client répond à une question sur la création ou la modification d'un dossier (« Faut-il quand même créer… », « Confirmez-vous … sans cette vérification ») → ce n'est pas pour Experio_repondre : reprends la compétence « Nouveau dossier » ou « Modification de dossier » et rappelle son outil comme elle le dit.

ÉTAPES :
1. Experio_repondre, une seule fois, sans paramètre : la réponse est le message du client tel qu'il l'a écrit.
2. Recopie sa réponse mot pour mot, facture par facture. Garde les numéros exacts.

QUAND Experio_repondre RÉPOND :
- Il demande de quelle facture ou de quel relevé il s'agit → pose cette question au client telle quelle.
- Il pose une nouvelle question → pose-la au client telle quelle.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- reformuler la réponse du client ou la compléter ;
- répondre toi-même à la question à la place du client ;
- appeler Experio_traiter_fichiers pour donner une réponse.
