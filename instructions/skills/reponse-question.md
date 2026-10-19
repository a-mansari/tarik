---
name: Réponse à une question
description: Le client répond à une question que Tarik a posée pendant le traitement de ses factures ou relevés (le tiers, le compte, la TVA, une retenue à la source, une charge constatée d'avance, l'exercice, le compte bancaire d'un relevé, la banque d'un journal), ou demande de reprendre ce traitement. À charger dès que le message du client est une telle réponse.
---
DÉCIDE TOUT DE SUITE :
- Le message du client répond à une question que le traitement a posée plus haut (une ligne « QUESTION : » dans une réponse d'Experio_traiter_fichiers ou d'Experio_repondre) sur une facture, un relevé ou un journal de banque (« le compte 6125 », « oui », « non recuperable », « à la charge de la société », « l'exercice 2025 », « le compte BMCE », « CIH ») → appelle Experio_repondre.
- Le client demande de reprendre ou de finir le traitement de ses factures → appelle Experio_repondre.
- Le message est une nouvelle demande (de nouvelles factures, un autre dossier) → ce n'est pas une réponse : suis la compétence de cette demande.
- Le client répond à la question sur le dossier, ou sur achat / vente (même sur une ligne « QUESTION : » : elle n'attend pas de réponse à Experio_repondre) → ce n'est pas pour Experio_repondre : continue « Traitement des documents » et appelle Experio_traiter_fichiers.
- Le client répond à une question sur la création ou la modification d'un dossier (« Faut-il quand même créer… », « Confirmez-vous … sans cette vérification ») → ce n'est pas pour Experio_repondre : reprends la compétence « Nouveau dossier » ou « Modification de dossier » et rappelle son outil comme elle le dit.

ÉTAPES :
1. Experio_repondre, une seule fois. Le message du client part tel qu'il l'a écrit ; toi, tu remplis seulement decision, d'après la dernière question posée et ce que le client y répond :
   - La question finit par « oui / non » → decision = « oui » ou « non », exactement, si le client répond clairement l'un ou l'autre. Lis bien la question : « la TVA est-elle non récupérable ? » → « non récupérable » se dit « oui », « elle est récupérable » se dit « non ».
   - La question propose des tiers (« Tiers proches : NOM (id 123…) ») → decision = l'id du tiers que le client choisit, le nombre après « id », seulement s'il en désigne un seul sans doute possible : par sa place (« le premier »), son ICE, ou un nom qu'un seul des tiers proposés porte. Un nom que plusieurs tiers proposés partagent (« Atlas » quand trois s'appellent ATLAS…) ne choisit rien → decision vide. Un « oui » seul, « aucun », « aucun des trois », « je ne sais pas » ne choisissent aucun tiers non plus → decision vide ; ne prends jamais le premier de la liste par défaut. Il donne à la place le nom ou l'ICE d'un autre tiers → decision = ce nom ou cet ICE, recopié tel qu'il l'a écrit.
   - La question porte sur l'exercice (« … ou je crée un nouvel exercice … (répondez « nouveau ») ? ») → decision = « nouveau » si le client veut un nouvel exercice, ou le nom de l'exercice ouvert qu'il choisit, recopié tel que la question l'écrit (« Exercice 2025 »). Un refus (« pas besoin de créer un exercice ») n'est pas « nouveau ». « La plus récente » est celui qui finit le plus tard, « la plus ancienne » celui qui commence le plus tôt. Quand la question liste plusieurs exercices ouverts, « dans l'exercice ouvert », « dans un des exercices » ou « comme facture antérieure » sans en nommer un ne désignent aucun exercice → decision vide ; ne prends jamais le premier de la liste par défaut.
   - La question demande le compte bancaire d'un relevé (« … lequel ? NOM - BANQUE (n° 41), … ») → decision = le numéro du compte qu'il choisit, le nombre après « n° », seul. Il le désigne par son nom, sa banque (« Attijari », « AWB », « la Banque Populaire ») ou sa place ; un autre nombre dans sa phrase (« j'ai 7 relevés ») n'est pas ce numéro. Une banque qui n'est pas dans la liste, ou deux comptes possibles → decision vide.
   - La question demande à quelle banque est un journal (« le journal BQ est a quelle banque ? NOM (n° 3), … ») → decision = le numéro de la banque qu'il désigne, le nombre après « n° », seul. Il la nomme par son nom, son sigle (« CIH », « AWB », « BP », « BMCE ») ou son ancien nom ; une banque qui n'est pas dans la liste → decision vide.
   - Toute autre question (un compte, une retenue, la date d'une facture) → decision vide : son message suffit.
   - Plusieurs questions attendent (plusieurs lignes « QUESTION : », ou Experio_repondre demande de quelle facture il s'agit) → about est obligatoire : about = le numéro de la facture à laquelle le client répond, le nombre après « facture » (« facture 12 » → 12), le nom du relevé, ou le nom du journal de banque. Il la désigne par ce numéro, par son fournisseur, ou seulement par ce qu'il répond : alors déduis la facture du genre de sa réponse, un compte (« Compte 6125 ») répond à la question du compte, un oui ou un non (« Non, elle est récupérable ») à la question oui / non, et mets ce numéro dans about même si le client ne l'écrit pas ; about reste vide seulement si deux questions attendent le même genre de réponse. Une seule question → about vide.
   - La réponse est floue, contradictoire (« oui… enfin non »), ou ne répond pas à la question → decision vide. Experio_repondre repose alors la question.
   - La réponse pose une condition (« si c'est un véhicule de tourisme, oui », « à condition que… », « ça dépend ») → decision vide, même si elle contient oui ou non : c'est au client de dire si la condition est remplie.
2. Recopie sa réponse mot pour mot, facture par facture. Garde les numéros exacts.

QUAND Experio_repondre RÉPOND :
- Il demande de quelle facture ou de quel relevé il s'agit → pose cette question au client telle quelle.
- Il pose une nouvelle question → pose-la au client telle quelle.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- deviner decision, ou la déduire d'autre chose que la réponse du client à cette question ;
- mettre dans decision un id qui n'est pas dans la question ;
- reformuler la réponse du client ou la compléter ;
- répondre toi-même à la question à la place du client ;
- appeler Experio_traiter_fichiers pour donner une réponse.
