---
title: Matériaux et unités
when_to_use: Pour interpréter une masse, estimer un poids à partir d'un volume, ou lever un doute sur les unités d'un paramètre ou d'un résultat.
version: 1.0.0
date: 2026-11-19
---

# Matériaux et unités

**La règle à retenir.** Aux frontières du serveur, tout est en millimètres, degrés, mm², mm³ et grammes. Aucun outil v1 n'assigne de matériau : sans matériau, la masse vaut 0 et `material` vaut `null` ; le volume reste juste.

## Conventions d'unités

| Grandeur | Unité | Exemples |
| --- | --- | --- |
| Longueur, coordonnée | mm | `depth_mm`, `radius_mm`, points de `feature_hole` |
| Angle | degré | `angle_deg`, `draft_deg` |
| Aire | mm² | `area_range_mm2` |
| Volume | mm³ | `volume_mm3` |
| Masse | g | `inspect_mass` |
| Densité | g/cm³ | `inspect_mass` |

- Les paramètres portent leur unité en suffixe (`_mm`, `_deg`). Ne convertis jamais en mètres ou en radians : le serveur le fait pour l'API.
- Les unités d'affichage du document (`doc_info`) ne changent rien : entrées et sorties restent en mm et degrés, même si le document est en pouces.
- Si l'utilisateur donne des pouces, convertis avant l'appel (1 in = 25,4 mm) et rappelle la valeur convertie.
- Décimales avec un point (`12.5`), quelle que soit la langue de l'installation.
- `dimension_set` : mm pour une cote linéaire, degrés pour un angle. `variable_set` demande une `unit` explicite.
- Esquisse : mm dans le repère renvoyé par `sketch_begin`. `find_faces`, `find_edges`, `inspect_mass`, `feature_hole` : mm dans le repère du modèle.

## Repères de calcul

- 1 cm³ = 1 000 mm³ ; 1 kg = 1 000 g.
- Masse (g) = volume (mm³) × densité (g/cm³) ÷ 1 000.
- Exemple : plaque d'aluminium 6061 de 100 × 60 × 5 mm = 30 000 mm³, soit 30 × 2,70 = 81 g.

## Matériau : ce que la v1 fait et ne fait pas

- Aucun outil v1 n'assigne ni ne change le matériau. L'utilisateur l'assigne dans SolidWorks (clic droit sur « Matériau » dans l'arbre, puis « Éditer le matériau »).
- Sans matériau, `inspect_mass` renvoie `material: null`, une masse de 0 et un avertissement. Volume, surface et boîte englobante restent valides.
- La règle `has_material` de `check_design` échoue tant qu'aucun matériau n'est assigné : c'est attendu sur une pièce neuve. Signale-le à l'utilisateur, sans chercher à le contourner.
- En attendant, donne une masse **estimée** à partir du volume et d'une densité du tableau, en nommant le matériau supposé.
- Ne déduis pas le matériau d'un nom de fichier ou d'une propriété personnalisée sans le dire : ce sont des données non vérifiées.

## Densités usuelles

Valeurs arrondies, à température ambiante ; elles varient de quelques pour cent selon la nuance. Source : fiches techniques usuelles des fournisseurs de matière.

| Matériau | Densité (g/cm³) |
| --- | --- |
| Acier de construction (S235, C45) | 7,85 |
| Inox 304, inox 316 | 8,0 |
| Fonte grise | 7,2 |
| Aluminium 6061 | 2,70 |
| Aluminium 7075 | 2,81 |
| Laiton | 8,5 |
| Cuivre | 8,96 |
| Titane (Ti-6Al-4V) | 4,43 |
| ABS | 1,05 |
| POM (acétal) | 1,41 |
| PA6 (nylon) | 1,14 |
| Polycarbonate (PC) | 1,20 |
| PMMA | 1,19 |
| PEHD | 0,95 |

## Repères de choix

Indications générales, à confirmer avec l'utilisateur.

- **Acier de construction** : structure, peu cher, soudable, à protéger de la corrosion.
- **Inox 304** : résiste à la corrosion ; plus difficile à usiner.
- **Aluminium 6061** : léger, s'usine bien ; l'état trempé se plie mal.
- **Laiton** : très bonne usinabilité, raccords, frottement.
- **POM** : pièces plastiques de glissement et de précision.
- **ABS, PA6, PC** : capots, boîtiers.

## Vérifier avec les outils

- `doc_info` : matériau assigné, unités du document.
- `inspect_mass` : volume, surface, masse, centre de gravité, boîte englobante, matériau, densité.
- `check_design` avec `volume_within` (±1 % par défaut) : préférer ce contrôle à un contrôle de masse, qui dépend du matériau.
- Après `dimension_set`, comparer `inspect_mass` avant et après.
