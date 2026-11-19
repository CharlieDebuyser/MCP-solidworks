---
title: Recettes par code d'erreur
when_to_use: Dès qu'un outil renvoie ok = false, pour trouver la cause probable et la correction selon le code d'erreur.
version: 1.0.0
date: 2026-11-19
---

# Recettes par code d'erreur

**La règle à retenir.** Lis `error.hint` et `state` avant d'agir. Ne rejoue le même appel que si `retryable` est vrai ; sinon inspecte, change un paramètre, puis réessaie. Après deux échecs identiques, arrête et explique à l'utilisateur.

## Connexion et disponibilité

### NOT_CONNECTED
Cause : pas de `sw_connect` dans la session, ou SolidWorks non lancé. Correction : `sw_connect` (`pid` s'il y a plusieurs instances), puis rejouer.

### SW_DISCONNECTED
Cause : SolidWorks fermé ou planté ; tous les handles sont invalidés. Correction : `sw_connect`, relire `inspect_tree`, redemander les handles, vérifier `state.sketch_open`.

### SW_BUSY
Cause : SolidWorks occupé, dialogue modal ouvert, ou file d'appels pleine. Correction : attendre quelques secondes et rejouer ; si cela persiste, demander à l'utilisateur de fermer le dialogue.

### TIMEOUT
Cause : opération trop longue ou dialogue bloquant ; elle a peut-être abouti. Correction : **ne pas rejouer**. Appeler `sw_status`, puis `inspect_tree` pour voir ce qui a été créé ; faire vérifier les dialogues ouverts.

### UNSUPPORTED_VERSION
Cause : fonction absente de cette version de SolidWorks. Correction : choisir une autre approche ou informer l'utilisateur.

## Document

### NO_ACTIVE_DOC
Cause : aucun document actif. Correction : `doc_list_open`, puis `doc_activate`, `doc_open` ou `doc_new`.

### WRONG_DOC_TYPE
Cause : outil de pièce appelé sur un assemblage ou une mise en plan (ou l'inverse), souvent parce que l'utilisateur a changé de fenêtre. Correction : `doc_info`, puis `doc_activate` sur le bon document.

### READ_ONLY_MODE
Cause : outil modifiant sur un document ouvert en lecture seule (défaut de `doc_open`) ou serveur en lecture seule. Correction : avec l'accord de l'utilisateur, rouvrir avec `read_only = false`. Si la restriction vient de la configuration (`sw_status`), l'expliquer : elle ne se change pas par les outils.

### PATH_DENIED
Cause : chemin hors des dossiers autorisés, avec `..`, chemin réseau, ou extension refusée. Correction : lire les dossiers autorisés dans `sw_status` et proposer un chemin à l'intérieur. Ne pas chercher à écrire ailleurs.

## Paramètres et entités

### INVALID_INPUT
Cause : paramètre manquant, hors bornes ou incohérent (valeur négative, `radius_mm` et `diameter_mm` ensemble, modèle absent pour `doc_new`). Correction : corriger le paramètre nommé dans `message` ; vérifier mm et degrés.

### ENTITY_STALE
Cause : handle inventé, d'un autre document, ou invalidé par une fonction qui a changé la géométrie, une reconnexion ou une fermeture. Correction : relancer `find_faces` ou `find_edges` (`sketch_inspect` pour un `ent:sk:<n>`).

### SELECTION_FAILED
Cause : entité non sélectionnable : fonction supprimée (suppressed), type inadapté (face non plane pour `sketch_begin`, arête au lieu d'une face). Correction : vérifier le type dans les descripteurs de `find_faces` ou `find_edges` et l'état de la fonction dans `inspect_tree`.

## Esquisse et fonctions

### SKETCH_OPEN
Cause : outil de fonction ou nouveau `sketch_begin` alors qu'une esquisse est ouverte. Correction : `sketch_end`, lire le statut, rejouer.

### SKETCH_NOT_CLOSED
Cause : contour ouvert, extrémités non coïncidentes, segments dupliqués ou sécants. Correction : `sketch_inspect`, fermer le contour ou ajouter `coincident` par `sketch_add_relation`, retirer les doublons. Voir `sketch-practices`.

### FEATURE_FAILED
Cause : l'API n'a rien créé : esquisse invalide, direction vers le vide, enlèvement hors matière, rayon de congé trop grand, coque trop épaisse, profil de révolution traversant l'axe. Correction : lire le diagnostic joint, corriger la cause, réessayer. Voir `features-guide`.

### REBUILD_FAILED
Cause : reconstruction en erreur, souvent après `dimension_set`, `feature_suppress` ou `feature_delete` : une fonction aval a perdu sa référence. Correction : `inspect_rebuild` ; la réponse dit si la fonction fautive est conservée ou annulée. Rétablir l'ancienne valeur ou corriger la fonction aval ; en dernier recours, `checkpoint_restore`.

## Garde-fous

### CONFIRMATION_REQUIRED
Cause : outil destructif (`feature_delete`, `doc_close` sans enregistrer, écrasement par `export_file`) appelé sans jeton ; ce n'est pas une panne. Correction : présenter l'effet à l'utilisateur, attendre son accord, rappeler avec les mêmes paramètres et le même `confirm_token` (120 secondes, usage unique). Jeton expiré : refaire l'appel sans jeton. Ne jamais confirmer sans accord.

### INTERNAL
Cause : erreur inattendue du serveur. Correction : ne pas rejouer en boucle ; vérifier l'état par `sw_status`, tenter une fois une autre approche, sinon transmettre le message à l'utilisateur (détail dans le journal).
