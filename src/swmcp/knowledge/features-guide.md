---
title: Guide des fonctions
when_to_use: Au moment de choisir une fonction (extrusion, révolution, perçage, congé, coque, répétition) ou quand une fonction échoue.
version: 1.0.0
date: 2026-11-19
---

# Guide des fonctions

**La règle à retenir.** Choisis la fonction qui exprime l'intention avec l'esquisse la plus simple, crée les volumes avant les finitions, et contrôle la variation de volume après chaque fonction : elle doit aller dans le sens et l'ordre de grandeur prévus.

## Choisir la fonction

| Besoin | Fonction |
| --- | --- |
| Forme prismatique, plaque, bossage | `feature_extrude` |
| Poche, rainure, découpe | `feature_cut_extrude` |
| Pièce de révolution (axe, bague) | `feature_revolve` (`cut` pour une gorge) |
| Perçages cylindriques | `feature_hole` |
| Arête arrondie ou cassée | `feature_fillet`, `feature_chamfer` |
| Pièce creuse à paroi constante | `feature_shell` |
| Motif répété | `feature_pattern_linear`, `feature_pattern_circular` |
| Pièce symétrique | `feature_mirror` |

Tous acceptent `name` (à renseigner) et `verify` : `"basic"` par défaut, `"full"` en fin d'étape. Coque, répétitions et symétrie peuvent manquer selon la version du serveur : s'en tenir aux outils listés.

## Extrusion et enlèvement

- `end_condition` : `blind` (profondeur `depth_mm`), `mid_plane` (symétrique autour du plan), `through_all`, `up_to_next`.
- `direction` : `normal` suit la normale renvoyée par `sketch_begin`, `reverse` l'inverse, `both` va des deux côtés. Un enlèvement dessiné sur une face va à l'opposé de sa normale extérieure : si le volume ne diminue pas, inverse la direction.
- `merge` actif pour garder un seul corps. Un ajout qui ne touche pas la matière crée un second corps.
- `draft_deg` : dépouille (draft), pour les pièces moulées.
- Pour un enlèvement traversant, préfère `through_all` à une profondeur « assez grande ».

## Révolution

- Profil fermé, d'un seul côté de l'axe, sans le traverser.
- `axis` : un segment de l'esquisse (`ent:sk:<n>`) ; `angle_deg` vaut 360 par défaut.
- Contrôle : cylindre plein, volume = π × r² × h.

## Perçages

- `feature_hole` : `face` (handle de `find_faces`), `points[]`, `diameter_mm`, puis `depth_mm` ou `through`.
- Les points sont en coordonnées **modèle** (mm), projetés sur la face : aucune conversion vers un repère d'esquisse.
- En v1 : cercles puis enlèvement extrudé, donc fond plat, sans taraudage ni lamage. Préviens l'utilisateur si un filetage est attendu.
- Contrôle : un perçage traversant retire π × d² / 4 × épaisseur.

## Congés et chanfreins

- Arêtes obtenues par `find_edges` (`linear`, `circular`, `adjacent_to`, `length_range_mm`, `near_point_mm`) juste avant l'appel.
- Chaque congé modifie les arêtes voisines : relancer `find_edges` avant le suivant.
- En fin d'arbre, du plus grand rayon au plus petit.
- Chanfrein : `distance_mm`, et `angle_deg` si différent de 45°.

## Coque, répétitions, symétrie

- Coque : `faces_to_remove[]` pour les faces ouvertes, `thickness_mm` inférieure au plus petit rayon concave de la pièce.
- Répétition : fonctions nommées (`features[]`), direction ou axe, nombre et pas ; vérifier que pas × (nombre − 1) reste dans la matière.
- Symétrie : il faut un vrai plan de symétrie, donc une base centrée sur l'origine.

## Erreurs classiques

| Symptôme | Cause probable | Correction |
| --- | --- | --- |
| `FEATURE_FAILED` sur un congé | Rayon supérieur à l'épaisseur de la paroi ou à la face voisine | Rayon < plus petite épaisseur adjacente |
| `FEATURE_FAILED` sur une coque | Coque après des congés de rayon inférieur à l'épaisseur | Coque avant ces congés, ou `thickness_mm` réduite |
| Volume inchangé après un enlèvement | Direction vers le vide | `direction = "reverse"` |
| Deux corps au lieu d'un | Ajout disjoint, ou `merge` désactivé | Faire toucher la base ; `merge` actif |
| `SKETCH_NOT_CLOSED` | Contour ouvert ou segments doublés | `sketch_inspect`, fermer le contour |
| `ENTITY_STALE` | Handle antérieur à un changement de géométrie | Relancer `find_faces` ou `find_edges` |
| Répétition en erreur | Occurrence hors de la matière | Réduire le nombre ou le pas |
| Perçage mal placé | Points donnés dans le repère de l'esquisse | Points en coordonnées modèle |

Après un échec, la réponse dit si la fonction fautive est conservée (en erreur) ou annulée : lis `state` et `inspect_rebuild`, et ne rejoue jamais le même appel sans avoir changé quelque chose.
