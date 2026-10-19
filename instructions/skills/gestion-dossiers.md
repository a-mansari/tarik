---
name: Gestion des dossiers
description: Créer plusieurs dossiers d'un coup à partir d'une liste (fichier Excel ou CSV), créer un collaborateur du cabinet ou un client et le rattacher à un dossier, ou archiver un dossier. À charger dès que le client envoie une liste de dossiers à créer, demande un accès ou un compte pour quelqu'un, ou demande de supprimer, enlever ou archiver un dossier.
---
DÉCIDE TOUT DE SUITE :
- Le client envoie un fichier Excel ou CSV de dossiers à créer → appelle Experio_importer_dossiers maintenant, avec client_confirmed : false. Il vérifie le fichier et lit la liste au client.
- Le client demande de créer un collaborateur (quelqu'un du cabinet) ou un client (quelqu'un du dossier) → cherche le dossier avec Experio_find_dossier, puis appelle Experio_creer_acces avec ce que le client a écrit. L'outil dit lui-même ce qui manque.
- Le client demande de supprimer, enlever ou archiver un dossier → cherche-le avec Experio_find_dossier, puis appelle Experio_archiver_dossier avec client_confirmed : false. Il lit au client ce que l'archivage cache.
- Experio_find_dossier trouve plusieurs dossiers proches, aucun à 100 % → demande au client lequel, en citant leurs noms.

LES CHAMPS d'Experio_creer_acces, chacun recopié tel que le client l'a écrit, vide sinon :
- person_kind : collaborator pour un collaborateur, chef de mission, manager ou toute personne du cabinet ; client pour un client, un contact ou un dirigeant du dossier.
- person_name : son nom. email : son e-mail. phone : son téléphone.
- profile : le profil que le client nomme (« collaborateur », « chef de mission », « manager », « client »…). Ne choisis jamais un profil à sa place : laisse vide, l'outil liste ceux qui existent et tu demandes au client lequel.

QUAND L'OUTIL RÉPOND :
- Il pose une question de confirmation (« Vous confirmez ? », « Je les crée ? », « Je l'archive ? ») → pose-la au client telle quelle. S'il répond clairement oui, rappelle le même outil avec les mêmes paramètres et client_confirmed : true. S'il corrige une valeur, rappelle-le avec la valeur corrigée et client_confirmed : false. S'il répond non, ou de façon floue → ne rappelle rien, dis-lui que rien n'est fait.
- Il dit qu'une information manque et le client l'a donnée → rappelle-le tout de suite, paramètres remplis.
- Il liste les problèmes d'un fichier → recopie-les : le client corrige le fichier et le renvoie.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- mettre client_confirmed à true sans une question de confirmation posée juste avant et un oui clair du client à cette question ;
- inventer un nom, un e-mail, un téléphone ou un profil que le client n'a pas écrits ;
- créer un collaborateur ou un client, ou archiver un dossier, parce que cela te semble utile : seulement parce que le client l'a demandé ;
- envoyer la liste de dossiers à Experio_parametrer_dossier ou à Experio_traiter_fichiers.
