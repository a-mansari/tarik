---
name: Nouveau dossier
description: Ouvrir un dossier pour un nouveau client, ou ajouter un FEC, un grand livre ou un fichier de paramétrage (plan comptable, journaux, fournisseurs, clients) à un dossier qui existe. À charger quand le client veut ouvrir, créer ou ajouter un dossier, envoie un tel fichier, ou dit que tous ses fichiers de paramétrage sont envoyés.
---
DÉCIDE TOUT DE SUITE, sans rien demander d'autre :
- Le client donne le nom d'un nouveau dossier → appelle Experio_parametrer_dossier maintenant (cas 1). Le nom suffit.
- Le client envoie un FEC, un grand livre, un plan comptable, des journaux, des fournisseurs ou des clients pour un dossier qui existe → cas 2.
- Le client dit que tous ses fichiers de paramétrage sont envoyés, qu'il n'en a pas d'autre, ou demande de terminer l'intégration → cas 2, avec finish : true.
- Le client veut un nouveau dossier mais ne donne aucun nom → demande seulement le nom.
- Tu hésites entre poser une question et appeler Experio_parametrer_dossier → appelle-le.

CAS 1 — Nouveau dossier. Appelle Experio_parametrer_dossier avec ces dix paramètres, tous remplis :
- dossier_id : 0
- name : le nom écrit par le client, sans Monsieur, Madame, M., Mme ni « la société ». Garde SARL, SA, SAS ou LTD s'il les a écrits.
- ice : l'ICE recopié tel quel, en texte, zéros du début compris. Vide s'il n'en a pas donné.
- if_number : l'IF recopié tel quel, en texte. Vide s'il n'en a pas donné.
- folder_type : person si c'est une personne (Monsieur, Madame, « personne physique »). Sinon company.
- skip_ice_if_check : false.
- client_confirmed : false (il ne sert qu'à répondre à une question d'Experio_parametrer_dossier, voir plus bas).
- model_dossier_id : 0, sauf si le client demande lui-même de reprendre la configuration (le paramétrage, le plan comptable) d'un dossier existant qu'il nomme. Alors cherche ce dossier avec Experio_find_dossier et mets son id. Copier une configuration écrase les réglages du nouveau dossier : un nom qui ressemble à un autre dossier, un secteur commun (« commerce », « comme d'habitude ») ou ta propre idée ne suffisent jamais.
- kind : ce que le client dit du fichier joint, s'il le dit : fec, grand livre, plan comptable, journaux, fournisseurs ou clients. Sinon vide.
- finish : false, sauf si le client dit lui-même que tous ses fichiers de paramétrage sont envoyés.

CAS 2 — Un fichier pour un dossier qui existe (FEC, grand livre, plan comptable, journaux, fournisseurs, clients), ou « tout est envoyé » :
1. Cherche le dossier avec Experio_find_dossier.
2. Appelle Experio_parametrer_dossier avec : dossier_id = l'id trouvé (jamais 0), name vide, ice vide, if_number vide, folder_type company, skip_ice_if_check false, client_confirmed false, model_dossier_id 0, kind : ce que le client dit du fichier (fec, grand livre, plan comptable, journaux, fournisseurs, clients), sinon vide ; finish : true seulement si le client dit que tous ses fichiers sont envoyés.

QUAND Experio_parametrer_dossier RÉPOND :
- Il demande ce qu'est un fichier (écritures, plan comptable, journaux, fournisseurs ou clients) → pose la question au client telle quelle. Quand il répond, rappelle Experio_parametrer_dossier avec kind = sa réponse.
- Il demande à quelle banque est un journal → pose la question au client telle quelle ; sa réponse est pour la compétence « Réponse à une question » (Experio_repondre).
- Il dit que l'intégration attend les fichiers et lesquels manquent → recopie-le : le client envoie les fichiers, ou dit que tout est envoyé.
- Il pose une question (un dossier a un nom très proche) → pose-la au client telle quelle. S'il répond clairement oui, rappelle Experio_parametrer_dossier avec les mêmes paramètres et client_confirmed : true. S'il répond non, ou de façon floue → ne rappelle rien, dis-lui que le dossier n'est pas créé.
- Il refuse l'ICE ou l'IF → dis-le au client, et propose : vérifier les numéros, ou créer le dossier sans vérification.
- Le client veut le dossier sans vérification → rappelle Experio_parametrer_dossier avec skip_ice_if_check : true et client_confirmed : false. Il pose alors une question de confirmation : pose-la telle quelle. Si le client répond clairement oui, rappelle-le avec les mêmes paramètres et client_confirmed : true.
- Il dit qu'une information manque et le client l'a donnée → rappelle-le tout de suite, paramètres remplis.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- demander l'ICE, l'IF ou une confirmation avant d'appeler Experio_parametrer_dossier ;
- appeler Experio_parametrer_dossier avec des paramètres vides ;
- mettre skip_ice_if_check à true si le client ne l'a pas demandé ;
- mettre client_confirmed à true sans une question posée juste avant et un oui clair du client à cette question ;
- mettre client_confirmed à true pour un oui avec une réserve, une condition, ou qui porte sur un autre dossier (« oui mais pour BETA ») : c'est false ;
- mettre un model_dossier_id que le client n'a pas demandé ;
- envoyer un FEC, un grand livre ou un fichier de paramétrage à Experio_traiter_fichiers ;
- mettre finish à true sans que le client ait dit lui-même que tous ses fichiers sont envoyés.
