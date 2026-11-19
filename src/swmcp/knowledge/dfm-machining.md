---
title: Conception pour l'usinage
when_to_use: Quand la pièce sera fraisée ou tournée, pour choisir rayons, profondeurs, épaisseurs et perçages réalisables.
version: 1.0.0
date: 2026-11-19
---

# Conception pour l'usinage (fraisage et tournage)

> **Valeurs indicatives à faire valider par un mécanicien.** Elles dépendent de la machine, de l'outillage, de la matière et de l'atelier.

**La règle à retenir.** Un outil de coupe est rond et de longueur limitée : tout angle intérieur a un rayon, toute poche et tout perçage ont une profondeur raisonnable par rapport à leur largeur ou à leur diamètre. Ne serre une tolérance que là où la fonction l'exige.

Sources citées : « atelier » = règle d'atelier courante ; « guides » = guides de conception des fabricants d'usinage à la demande.

## Angles intérieurs et poches

| Règle | Valeur prudente | Source |
| --- | --- | --- |
| Rayon des angles intérieurs verticaux | Au moins 1/3 de la profondeur de poche ; viser 3 mm ou plus | Atelier, guides |
| Rayon un peu supérieur à celui de la fraise | 3,2 mm pour une fraise de diamètre 6 | Atelier |
| Profondeur de poche | Jusqu'à 3 à 4 fois le diamètre de la fraise | Guides |
| Fond de poche | Plat, ou rayon de 0,5 à 1 mm | Atelier |
| Angle vif nécessaire (logement d'une pièce carrée) | Dégagement d'angle plutôt qu'angle vif | Atelier |

Les arêtes extérieures peuvent rester vives : préférer un chanfrein (`feature_chamfer`, 0,5 mm à 45° typique) à un congé extérieur, plus long à usiner.

## Parois

| Règle | Valeur prudente | Source |
| --- | --- | --- |
| Épaisseur minimale, métaux | 0,8 mm ; viser 1,5 mm ou plus | Guides |
| Épaisseur minimale, plastiques | 1,5 mm ; viser 2 mm ou plus | Guides |
| Hauteur d'une paroi ou nervure fine | Au plus 4 fois son épaisseur environ | Atelier |

## Perçages et taraudages

| Règle | Valeur prudente | Source |
| --- | --- | --- |
| Diamètres | Forets normalisés (pas de 0,5 ou 1 mm) | Atelier |
| Profondeur de perçage | Jusqu'à 4 fois le diamètre ; 10 fois avec outillage adapté | Guides |
| Fond de trou borgne | Conique (pointe de foret, environ 118°) ; le fond plat coûte plus cher | Atelier |
| Diamètre minimal | 1 mm couramment | Guides |
| Longueur de filet utile | 1,5 à 2 fois le diamètre nominal ; aucun gain au-delà de 3 fois | Atelier |
| Trou borgne taraudé | Perçage plus profond que le filet, d'environ 1,5 fois le diamètre | Atelier |
| Distance au bord | Au moins un diamètre de matière entre trou et bord | Atelier |

`feature_hole` crée en v1 un trou cylindrique à fond plat, sans filetage : liste dans le résumé les taraudages attendus.

## Tolérances

- Par défaut, tolérances générales de la norme ISO 2768, classe moyenne (m) : ±0,1 mm jusqu'à 6 mm, ±0,2 mm de 6 à 30 mm, ±0,3 mm de 30 à 120 mm, ±0,5 mm de 120 à 400 mm.
- Une tolérance plus serrée (ajustement, portée de roulement) se limite aux surfaces fonctionnelles et se décide avec le mécanicien.
- Le modèle se construit aux cotes nominales ; les tolérances se portent sur le plan, hors périmètre des outils v1.

## Accessibilité et prises de pièce

- Limiter les posages : regrouper les formes sur les mêmes faces ; une pièce usinable par le dessus seul est la moins chère. (Atelier.)
- Pas de contre-dépouille ni de forme inaccessible à un outil droit sans besoin validé avec l'atelier.
- Prévoir des faces planes et parallèles pour le serrage en étau.
- Partir de bruts du commerce (plats, ronds) quand la fonction le permet.
- Tournage : rayon de 0,2 à 0,4 mm en fond de gorge et d'épaulement ; longueur non soutenue au plus 8 à 10 fois le diamètre. (Atelier.)

## Contrôler avec les outils

- `find_edges` avec `circular` ou `radius_mm` : repérer les rayons intérieurs trop petits.
- `inspect_measure` entre deux faces : épaisseur de paroi, distance trou–bord.
- `inspect_dimensions` : relire diamètres et profondeurs, calculer les rapports.
- `check_design` avec `bbox_within` : la pièce tient dans le brut prévu.

Dans le résumé final, liste les règles vérifiées et non vérifiées, et rappelle que la validation par un mécanicien reste nécessaire avant fabrication.
