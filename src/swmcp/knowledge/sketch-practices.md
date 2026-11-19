---
title: Bonnes pratiques d'esquisse
when_to_use: Avant de dessiner une esquisse, ou quand une esquisse reste sous-contrainte, sur-contrainte ou non fermée.
version: 1.0.0
date: 2026-11-19
---

# Bonnes pratiques d'esquisse

**La règle à retenir.** Un contour fermé, sans doublon ni chevauchement, placé d'après le repère renvoyé par `sketch_begin`, contraint par des relations puis par des cotes, jusqu'à ce que `sketch_end` annonce « entièrement contrainte ».

## 1. Lire le repère, ne pas le supposer

`sketch_begin` renvoie l'origine, les axes X et Y et la normale de l'esquisse dans le repère du modèle. Les primitives prennent des coordonnées en mm **dans le repère de l'esquisse**.

Conventions habituelles des plans de référence :

| `plane` | Plan du modèle | X esquisse | Y esquisse | Normale |
| --- | --- | --- | --- | --- |
| `front` | XY | +X | +Y | +Z |
| `top` | XZ | +X | −Z | +Y |
| `right` | YZ | −Z | +Y | +X |

Sur une face (`ent:face:<n>`), l'orientation n'est pas prévisible : utilise le repère renvoyé pour convertir un point du modèle en coordonnées d'esquisse. Exemple sur `top` : le point d'esquisse (10, 5) est en X = 10, Z = −5 dans le modèle.

## 2. Séquence type

1. `sketch_begin` (`plane`, `name`) ; lire le repère.
2. Primitives : `sketch_add_rect`, `sketch_add_circle`, `sketch_add_line`, `sketch_add_polyline`, `sketch_add_arc`, `sketch_add_slot`, `sketch_add_point`. Conserver les handles `ent:sk:<n>` renvoyés (segments et points).
3. Relations : `sketch_add_relation`.
4. Cotes : `sketch_add_dimension`, avec `name` pour les cotes pilotes.
5. `sketch_end` ; lire le statut. En cas de doute, `sketch_inspect`.

Tant que l'esquisse est ouverte, les outils de fonction répondent `SKETCH_OPEN`.

## 3. Contour fermé, sans doublon

- Chaque contour de profil forme une boucle fermée : le dernier point coïncide avec le premier.
- Préfère `sketch_add_rect`, `sketch_add_circle`, `sketch_add_slot` ou `sketch_add_polyline` à une suite de `sketch_add_line`.
- Jamais de segments superposés, sécants ou partiellement chevauchants, ni de segment isolé.
- Un contour intérieur (cercle dans un rectangle) crée un trou ; les contours ne se touchent pas.
- Pour une révolution, le profil reste d'un seul côté de l'axe.

## 4. Relations avant cotes

L'inférence automatique est désactivée : aucune relation n'apparaît seule. Deux extrémités aux mêmes coordonnées ne sont liées que si l'outil les relie (polyligne, rectangle) ou si tu ajoutes `coincident`.

- `sketch_add_rect` pose lui-même les relations horizontales et verticales de ses côtés.
- Pour lignes et polylignes, ajoute `horizontal`, `vertical`, `parallel`, `perpendicular` explicitement.
- Autres types : `coincident`, `tangent`, `equal`, `concentric`, `symmetric`, `midpoint`, `fix`.
- Pose la géométrie (relations) puis les tailles (cotes). Une relation `equal` remplace une cote répétée.
- Évite `fix` : il fige sans exprimer l'intention.

## 5. Ancrer à l'origine

Une esquisse cotée reste sous-contrainte tant qu'elle peut glisser. Lie-la à l'origine, désignée par l'entité littérale `"origin"` :

- rectangle centré : `sketch_add_rect` en `mode` center à (0, 0), puis `coincident` entre son point central et `"origin"` ;
- cercle centré : `coincident` entre le centre et `"origin"` ;
- coin sur l'origine : `coincident` entre le sommet et `"origin"` ;
- sinon : deux cotes `distance` entre un point du contour et `"origin"`.

Centre le profil sur l'origine dès que la pièce est symétrique.

## 6. Coter juste

- Une cote par intention : `length`, `diameter`, `radius`, `angle`, `distance`.
- Rectangle ancré : deux cotes. Cercle ancré : une cote.
- Diamètre pour les cercles, rayon pour les arcs.
- Ne cote pas ce qu'une relation fixe déjà : l'esquisse deviendrait sur-contrainte.

## 7. Lire le statut de `sketch_end`

| Statut | Action |
| --- | --- |
| Entièrement contrainte | Créer la fonction |
| Sous-contrainte | `sketch_inspect`, puis ancrage, relation ou cote manquante |
| Sur-contrainte | Retirer la dernière relation ou cote ; chercher une cote qui double une relation |
| Échec | `sketch_inspect`, corriger ou refaire l'esquisse |

Une esquisse sous-contrainte peut donner une fonction correcte, mais elle bougera à la première modification ; la règle `sketches_fully_defined` de `check_design` la signale.

## 8. Rester simple

- Pas de congé ni de chanfrein d'esquisse : `feature_fillet`, `feature_chamfer`.
- Pas de motif répété à la main : une occurrence, puis une répétition de fonction.
- Pour des perçages, préférer `feature_hole`, dont les points sont en coordonnées **modèle** (mm), projetés sur la face.
