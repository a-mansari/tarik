---
name: Nouveau dossier
description: Ouvrir un dossier pour un nouveau client, ou ajouter un FEC ou un grand livre à un dossier qui existe. À charger quand le client veut ouvrir, créer ou ajouter un dossier, ou envoie un FEC ou un grand livre.
---
DÉCIDE TOUT DE SUITE, sans rien demander d'autre :
- Le client donne le nom d'un nouveau dossier → appelle Experio_parametrer_dossier maintenant (cas 1). Le nom suffit.
- Le client envoie un FEC ou un grand livre pour un dossier qui existe → cas 2.
- Le client veut un nouveau dossier mais ne donne aucun nom → demande seulement le nom.
- Tu hésites entre poser une question et appeler Experio_parametrer_dossier → appelle-le.

CAS 1 — Nouveau dossier. Appelle Experio_parametrer_dossier avec ces huit paramètres, tous remplis :
- dossier_id : 0
- name : le nom écrit par le client, sans Monsieur, Madame, M., Mme ni « la société ». Garde SARL, SA, SAS ou LTD s'il les a écrits.
- ice : l'ICE recopié tel quel, en texte, zéros du début compris. Vide s'il n'en a pas donné.
- if_number : l'IF recopié tel quel, en texte. Vide s'il n'en a pas donné.
- folder_type : person si c'est une personne (Monsieur, Madame, « personne physique »). Sinon company.
- skip_ice_if_check : false.
- model_dossier_id : 0. Seule exception : le client demande de reprendre la configuration d'un dossier existant. Alors cherche ce dossier avec Experio_find_dossier et mets son id.
- kind : fec ou grand livre si le client l'a dit. Sinon vide.

CAS 2 — FEC ou grand livre pour un dossier qui existe :
1. Cherche le dossier avec Experio_find_dossier.
2. Appelle Experio_parametrer_dossier avec : dossier_id = l'id trouvé (jamais 0), name vide, ice vide, if_number vide, folder_type company, skip_ice_if_check false, model_dossier_id 0, kind : fec ou grand livre si le client l'a dit, sinon vide.

QUAND Experio_parametrer_dossier RÉPOND :
- Il pose une question (un dossier a un nom très proche) → pose-la au client telle quelle. S'il répond oui, rappelle Experio_parametrer_dossier avec exactement les mêmes paramètres.
- Il refuse l'ICE ou l'IF → dis-le au client, et propose : vérifier les numéros, ou créer le dossier sans vérification.
- Le client écrit lui-même qu'il veut le dossier sans vérification → rappelle Experio_parametrer_dossier avec skip_ice_if_check : true.
- Il dit qu'une information manque et le client l'a donnée → rappelle-le tout de suite, paramètres remplis.
- Sinon → recopie sa réponse mot pour mot.

NE JAMAIS :
- demander l'ICE, l'IF ou une confirmation avant d'appeler Experio_parametrer_dossier ;
- appeler Experio_parametrer_dossier avec des paramètres vides ;
- mettre skip_ice_if_check à true si le client ne l'a pas écrit ;
- envoyer un FEC ou un grand livre à Experio_traiter_fichiers.
