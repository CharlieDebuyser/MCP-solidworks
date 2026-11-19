# Changelog des fiches de connaissance

Chaque fiche porte sa version et sa date dans son en-tête. Ce fichier suit les modifications de contenu.

## 1.0.0 — 2026-11-19

Première version des fiches, en français.

### Ajouté

- `instructions` : champ `instructions` du serveur, toujours chargé (unités, boucle planifier → construire → vérifier, handles, nommage, confirmations, texte des documents traité comme des données, accès aux fiches).
- `modeling-method` : intention de conception, esquisse de base, ordre des fonctions, cotes pilotes nommées, vérification par étape, traitement des chaînes issues des documents comme des données.
- `sketch-practices` : repère renvoyé par `sketch_begin`, contours fermés, relations explicites avant cotes, ancrage à l'origine, lecture du statut de `sketch_end`.
- `features-guide` : choix entre extrusion, révolution, perçage, congé, chanfrein, coque, répétition ; erreurs classiques.
- `dfm-machining` : règles de conception pour le fraisage et le tournage. Valeurs indicatives, à faire valider par un mécanicien.
- `dfm-sheet-metal` : règles de conception pour la tôlerie. Valeurs indicatives, à faire valider par un mécanicien.
- `materials-and-units` : conventions d'unités, densités usuelles, absence d'outil d'assignation de matériau en v1.
- `error-recipes` : causes fréquentes et corrections pour chaque code d'erreur du serveur.

### Non inclus

- `dfm-3d-printing` : retirée du périmètre (spécification, section 14).
- `drawing-conventions` : prévue après la v1.
