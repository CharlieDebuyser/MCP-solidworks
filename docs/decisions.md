# Décisions d'implémentation et écarts par rapport à la spécification

## Écarts assumés

| Sujet | Spécification | Choix fait | Raison |
| --- | --- | --- | --- |
| SDK MCP | « SDK officiel, classe `FastMCP`, version épinglée » | `mcp>=1.30,<2` | La version 2 du SDK a renommé `FastMCP` en `MCPServer` et changé d'autres API ; la branche 1.x reste conforme à la lettre de la spec. Migration à planifier. |
| Unités dans `SwBackend` | « un outil convertit les unités, appelle le backend » | L'interface `SwBackend` est en mm/deg ; la conversion vers m/rad se fait dans `com_backend.py` via `units.py` | Un seul jeu de modèles de données, backend simulé lisible, et toujours un seul endroit pour les facteurs de conversion. |
| Transport HTTP | Décrit en §4, §8, §11 | Non implémenté | Décision de la §14 : stdio uniquement en v1. |
| `feature_hole.points` | Non précisé | Points en coordonnées du **modèle**, projetés sur la face | Le repère d'esquisse d'une face n'est pas connu du LLM avant `sketch_begin` (piège §10). |
| `doc_close` | Jalon M3, profil non précisé | Exposé dès le profil `inspect` (sans enregistrement) | L'audit et l'export par lot doivent pouvoir refermer ce qu'ils ouvrent. |
| `prop_get` | Profil `full` (« propriétés ») | `inspect` ; seul `prop_set` est en `full` | Lecture seule. |
| Fichier corrompu ou absent | « erreur codée » | `INVALID_INPUT` avec `details.open_errors` | Aucun code dédié dans la table de la §5. |
| `expected_doc` différent | Refus exigé, code non précisé | `INVALID_INPUT` avec `details.active_doc` | Idem. |
| Plafond d'appels dépassé | Non précisé | `SW_BUSY` (rejouable avec délai) | Idem. |
| Index d'API absent | Non précisé | Réponse `ok` avec `indexed=false` et un avertissement | Ce n'est pas une erreur de l'appelant. |
| `dimension_set` qui casse la reconstruction | « jamais d'état ambigu » | L'ancienne valeur est rétablie, erreur `REBUILD_FAILED` avec `details.reverted=true` | Transactionnel. |
| Tâches `sw-bench` | Énoncé + YAML d'attendus | Attendus en Python (`tests/eval/level1.py`) | Évite une dépendance YAML tant que seul le niveau 1 scripté existe. |

## Propositions d'outils (non implémentées : absentes du catalogue)

La consigne 4 de la §14 interdit d'improviser un outil. Deux manques ont été constatés :

1. **`doc_set_material(name)`.** Le scénario 1 demande une pièce « en acier », `check_design` a une
   règle `has_material` et `inspect_mass` renvoie une masse nulle sans matériau, mais aucun outil
   n'assigne de matériau. En l'état, le LLM doit demander à l'utilisateur de le faire dans SolidWorks.
2. **`doc_list_folder(path)`.** Le scénario 4 (« toutes les pièces du dossier X ») suppose de lister
   un dossier. Contournement livré : le prompt `batch_export` liste lui-même les modèles du dossier
   (contrôlé par la liste blanche). Un client sans prompts ni accès au système de fichiers ne peut
   pas jouer ce scénario.

## Limites du backend simulé

Exact pour les prismes (extrusions, enlèvements, perçages, répétitions) et les révolutions ; approché
pour les congés et chanfreins (arête convexe à 90°), la coque (proportionnelle à la boîte englobante)
et le solveur d'esquisse (décompte de degrés de liberté, propagation horizontale/verticale). Les
enlèvements ne réduisent pas la boîte englobante. Il ne reproduit aucun échec réel de SolidWorks.

## Portes de jalons (v1 sans test réel)

| Jalon | Critères vérifiables sans SolidWorks | Reporté au premier accès |
| --- | --- | --- |
| M0 | Serveur stdio, enveloppe, worker, `sw_connect`, `sw_status`, backend simulé, tests verts, stdout propre | `swmcp doctor` sur un poste de test |
| M1 | Documents, inspection, capture, export ; fichier absent ou corrompu → erreur codée | Ouverture d'une vraie pièce |
| M2 | Scénarios 1 et 2, niveau 1 de `sw-bench` scripté à 100 % (simulé, noms français) | Mêmes essais sur SolidWorks |
| M3 | Scénario 5, traversée de chemins, aucune action destructive sans jeton, six règles de `check_design` | — |
| M4 | `instructions`, ressources, prompts, `knowledge_get`, `api_search`, `api_describe` | `sw-bench` avec un LLM réel, ablation des fiches, validation dans Claude Desktop et Claude Code |
| M5 | Coque, répétitions, symétrie, `variable_set`, interférences, `api_call_raw`, `install-client`, scénario 4 | Installation sur poste vierge, version 1.0 |
