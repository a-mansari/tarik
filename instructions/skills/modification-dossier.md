---
name: Modification de dossier
description: Modifier un dossier qui existe déjà dans Experio : son nom, son type, son ICE, son IF, son RC, sa patente, sa T.S.C, son capital, son numéro CNSS ou son adresse. À charger dès qu'un client demande de changer, corriger, mettre à jour ou compléter une de ces informations d'un dossier.
---
DÉCIDE TOUT DE SUITE :
- Le client nomme le dossier et donne la nouvelle valeur → cherche le dossier avec Experio_find_dossier, puis appelle Experio_modifier_dossier dans ce même tour.
- Experio_find_dossier trouve plusieurs dossiers proches, aucun à 100 % → demande au client lequel, en citant leurs noms. N'appelle pas Experio_modifier_dossier.
- Le client ne nomme aucun dossier → demande-lui lequel.
- Le client veut changer un champ mais ne donne pas la nouvelle valeur → demande-lui seulement cette valeur.
- Le client demande autre chose que ces champs (configuration, comptes, collaborateurs...) → dis que tu ne peux pas encore le faire.

ÉTAPES :
1. Experio_find_dossier avec le nom du dossier.
2. Experio_modifier_dossier avec dossier_id = l'id trouvé, et seulement les champs que le client veut changer. Laisse tous les autres vides.
3. Recopie sa réponse mot pour mot.

LES CHAMPS, chacun recopié tel que le client l'a écrit :
- name : le nouveau nom, sans Monsieur, Madame, M., Mme ni « la société ».
- folder_type : company pour une société, person pour une personne physique.
- ice : le nouvel ICE, en texte, zéros du début compris.
- if_number : le nouvel IF, en texte.
- rc : le registre de commerce. patente : la patente. tsc : la T.S.C. cnss : le numéro CNSS.
- capital : le montant, en chiffres.
- street, street2 : l'adresse et son complément. city : la ville. zip : le code postal.
- skip_ice_if_check : false. true seulement si le client demande lui-même la modification sans vérification de l'ICE ou de l'IF.
- client_confirmed : false, sauf pour répondre à une question de confirmation (voir plus bas).

QUAND Experio_modifier_dossier RÉPOND :
- Il dit qu'un autre dossier a déjà cet ICE, cet IF ou ce nom → dis-le au client, sans rien changer d'autre.
- Il refuse l'ICE ou l'IF (registre officiel) → dis-le au client, et propose : vérifier le numéro, ou modifier sans vérification.
- Il dit qu'une valeur n'a pas été modifiée parce que le client ne l'a pas écrite → demande au client cette valeur exacte.
- Il dit qu'aucun champ n'a été donné → rappelle-le tout de suite avec les champs que le client a demandés.
- Il pose une question de confirmation (modification sans vérification) → pose-la telle quelle. Si le client répond clairement oui, rappelle-le avec les mêmes paramètres et client_confirmed : true. S'il répond non, ou de façon floue → ne rappelle rien.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- remplir un champ que le client n'a pas demandé de changer ;
- mettre client_confirmed à true sans une question posée juste avant et un oui clair du client à cette question ;
- mettre client_confirmed à true pour un oui avec une réserve, une condition, ou qui porte sur un autre dossier (« oui mais pour BETA ») : c'est false ;
- inventer, corriger ou compléter une valeur que le client n'a pas écrite ;
- appeler Experio_modifier_dossier sans l'id donné par Experio_find_dossier ;
- utiliser Experio_parametrer_dossier pour modifier un dossier existant.
