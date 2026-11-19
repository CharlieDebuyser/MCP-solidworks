---
title: Conception pour la tôlerie
when_to_use: Quand la pièce sera découpée et pliée dans une tôle, pour choisir épaisseur, rayons de pliage, bords et positions des trous.
version: 1.0.0
date: 2026-11-19
---

# Conception pour la tôlerie (découpe et pliage)

> **Valeurs indicatives à faire valider par un mécanicien.** Elles dépendent de la matière, de son état, du sens de laminage et de l'outillage du tôlier.

**La règle à retenir.** Une pièce de tôlerie a une seule épaisseur, des plis de rayon intérieur au moins égal à l'épaisseur, et des trous tenus à distance des plis et des bords. Elle doit se déplier à plat sans recouvrement.

Sources citées : « atelier » = règle d'atelier courante ; « guides » = guides de conception des tôliers. Dans les tableaux, e = épaisseur, r = rayon intérieur de pliage.

## Limite de la v1

Le serveur n'a pas d'outil de tôlerie (tôle de base, pli, déplié). Une pièce pliée se modélise comme un solide d'épaisseur constante. Le déplié reste à faire par l'utilisateur dans SolidWorks ou par le tôlier : dis-le dans le résumé.

## Épaisseur et plis

| Règle | Valeur prudente | Source |
| --- | --- | --- |
| Épaisseur | Constante sur toute la pièce | Atelier |
| Épaisseurs du commerce | 0,8 – 1 – 1,5 – 2 – 3 mm | Catalogues de fournisseurs |
| Rayon intérieur, acier doux, inox, aluminium de pliage (série 5000) | r ≥ 1 e | Atelier, guides |
| Rayon intérieur, aluminium trempé (6061-T6) | r ≥ 2 à 3 e, risque de fissure | Guides |
| Rayon de pliage | Le même pour tous les plis | Atelier |
| Longueur minimale d'un bord plié | 4 e, depuis l'extérieur du pli | Guides |
| Tolérance d'angle | ±1° usuel | Guides |
| Facteur K du déplié (information) | 0,33 à 0,5 ; à demander au tôlier | Atelier |

Éviter les plis successifs trop rapprochés (U étroit) : l'outil doit passer.

## Trous et découpes

| Règle | Valeur prudente | Source |
| --- | --- | --- |
| Diamètre minimal d'un trou | 1 e | Atelier |
| Largeur minimale d'une fente ou encoche | 1,5 e | Guides |
| Matière entre trou et bord, ou entre deux trous | 2 e | Guides |
| Distance du bord d'un trou à un pli | 2,5 e + r ; sinon le trou se déforme | Guides |
| Rayon des angles intérieurs de découpe | 0,5 e | Atelier |

## Dégagements de pliage

Quand un pli ne traverse pas toute la largeur, prévoir une encoche à chaque extrémité : largeur d'au moins e, profondeur d'au moins e + r. Sans elle, la tôle se déchire au coin du pli. (Guides.)

## Tolérances

- Découpe laser : ±0,1 à ±0,2 mm sur les contours. (Guides.)
- Cote traversant un pli : prévoir ±0,5 mm par pli. (Guides.)
- Tolérances générales : norme ISO 2768 par défaut.
- Placer les cotes fonctionnelles sur une même face plane, sans traverser de pli.

## Modéliser avec les outils v1

1. Esquisser le profil plié vu de côté, en épaisseur : contour fermé, épaisseur pilotée par une cote nommée (`Epaisseur`), relations `parallel` et `equal` entre les bords.
2. Extruder en `mid_plane` sur la largeur de la pièce (`feature_extrude`).
3. Arrondir les plis par `feature_fillet` : arête intérieure au rayon r, arête extérieure à r + e, pour garder l'épaisseur constante.
4. Percer avec `feature_hole` en `through`, points en coordonnées modèle.

## Contrôler

- `inspect_measure` entre les deux grandes faces de chaque aile : distance égale à l'épaisseur.
- `find_edges` avec `circular` et `radius_mm` : relire les rayons de pliage.
- `inspect_measure` entre l'arête d'un trou et un pli ou un bord : distances minimales.
- `check_design` : `single_body`, `bbox_within`, `volume_within` (volume proche de la surface dépliée × épaisseur).

Dans le résumé final, donne l'épaisseur, le rayon de pliage et les règles vérifiées, et rappelle que le déplié et la validation par le tôlier restent à faire.
