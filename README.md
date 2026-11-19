# swmcp — serveur MCP pour SolidWorks

Pilotez SolidWorks depuis Claude Desktop, Claude Code ou tout autre client MCP : créer et modifier
des pièces, inspecter des modèles, vérifier la géométrie, exporter.

Le serveur est local et déterministe. Il ne contient et n'appelle aucun LLM, n'ouvre aucune
connexion réseau et n'envoie aucune télémétrie : il expose des outils typés, valide les entrées,
exécute dans le SolidWorks du poste et renvoie un état mesurable.

> **État : v0.1.0, pas encore validé sur un vrai SolidWorks.**
> La couche MCP, les garde-fous et les 56 outils sont testés sur un backend simulé (230 tests).
> La couche COM qui parle à SolidWorks (`src/swmcp/backend/com_backend.py`) est écrite d'après
> l'API Help mais n'a jamais été exécutée : attendez-vous à des corrections au premier essai.
> Les appels concernés sont listés dans [docs/unverified.md](docs/unverified.md).
> Commencez par `swmcp doctor --connect`, puis `swmcp selftest` (voir plus bas).

## Sommaire

1. [Prérequis](#prérequis)
2. [Installation](#installation)
3. [Vérifier l'installation](#vérifier-linstallation)
4. [Brancher un client MCP](#brancher-un-client-mcp)
5. [Premier usage](#premier-usage)
6. [Ce que le serveur expose](#ce-que-le-serveur-expose)
7. [Configuration](#configuration)
8. [Sécurité](#sécurité)
9. [Essayer sans SolidWorks](#essayer-sans-solidworks)
10. [Dépannage](#dépannage)
11. [Développement](#développement)

## Prérequis

| Élément | Exigence |
| --- | --- |
| Système | Windows 10 ou 11, 64 bits |
| SolidWorks | Installé et sous licence sur le même poste, lancé au moins une fois (enregistrement COM). Versions récentes visées (2023 et suivantes), interface en français ou en anglais |
| Python | 3.11 ou plus, 64 bits |
| Installeur | [`uv`](https://docs.astral.sh/uv/) (recommandé) ou `pipx` |

Sur macOS ou Linux, seul le [backend simulé](#essayer-sans-solidworks) fonctionne.

## Installation

Depuis GitHub, en une commande :

```bash
uv tool install git+https://github.com/CharlieDebuyser/MCP-solidworks.git
```

Avec `pipx` :

```bash
pipx install git+https://github.com/CharlieDebuyser/MCP-solidworks.git
```

Depuis une copie locale du dépôt :

```bash
uv tool install .
```

La commande `swmcp` est alors disponible. Si elle n'est pas trouvée, lancez `uv tool update-shell`
puis rouvrez le terminal.

Mise à jour : `uv tool upgrade swmcp`. Désinstallation : `uv tool uninstall swmcp`.

## Vérifier l'installation

Ouvrez SolidWorks, fermez tout dialogue, puis lancez dans cet ordre :

```bash
swmcp doctor --connect
```

Le diagnostic contrôle Windows, Python, pywin32, l'enregistrement COM de SolidWorks, la connexion
à l'instance ouverte, le modèle de pièce par défaut et les droits sur les dossiers. Chaque échec
est suivi de la correction à faire.

```bash
swmcp selftest --out rapport.json
```

Le test automatique joue une séquence réelle dans SolidWorks (créer une pièce, esquisser,
extruder, percer, mesurer, capturer, enregistrer, exporter en STEP, fermer) et écrit un rapport
JSON, étape par étape. C'est le moyen le plus rapide de savoir ce qui marche sur votre poste.
Les fichiers produits vont dans `<dossier de travail>/selftest`.

```bash
swmcp index-api
```

Optionnel : construit l'index local de l'API SolidWorks à partir des bibliothèques de types du
poste. Il alimente les outils `api_search` et `api_describe`.

## Brancher un client MCP

### Claude Desktop

```bash
swmcp install-client claude-desktop --allowed-dir "C:\Projets\CAO"
```

La commande ajoute le serveur à `%APPDATA%\Claude\claude_desktop_config.json` après avoir
sauvegardé le fichier existant. Redémarrez Claude Desktop : le serveur `solidworks` apparaît dans
la liste des outils.

### Claude Code

Dans le dossier du projet :

```bash
swmcp install-client claude-code --allowed-dir "C:\Projets\CAO"
```

Cela écrit ou complète `.mcp.json` dans le dossier courant. Autre possibilité, pour tous vos
projets :

```bash
claude mcp add solidworks -- swmcp serve --profile design
```

### Autre client, ou configuration à la main

```bash
swmcp print-config --allowed-dir "C:\Projets\CAO"
```

affiche le bloc à copier dans la configuration du client :

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "swmcp",
      "args": ["serve", "--profile", "design"],
      "env": { "SWMCP_ALLOWED_DIRS": "C:\\Projets\\CAO" }
    }
  }
}
```

Si le client ne trouve pas la commande, remplacez `swmcp` par le chemin absolu de `swmcp.exe`
(`where swmcp` le donne). Le transport est stdio ; il n'y a pas de mode HTTP en v1.

## Premier usage

1. Ouvrez SolidWorks sur le poste. Le serveur se connecte à l'instance ouverte ; il ne la lance
   que si `SWMCP_LAUNCH_SW=1`.
2. Dans le client, demandez par exemple :

   > Connecte-toi à SolidWorks et crée un support en L de 60 × 40 mm, épaisseur 3 mm, largeur
   > 30 mm, avec deux perçages Ø5 à 10 mm des bords. Vérifie le résultat et montre-moi une vue iso.

   Le modèle enchaîne alors `sw_connect`, `doc_new`, les outils d'esquisse, `feature_extrude`,
   `find_faces`, `feature_hole`, `check_design` puis `view_capture`.

3. Pour modifier ensuite :

   > Passe l'épaisseur à 4 mm et dis-moi la variation de masse.

   Le modèle lit les cotes nommées (`inspect_dimensions`) et les change par `dimension_set`.

### Les quatre prompts fournis

Les clients qui affichent les prompts MCP proposent quatre workflows prêts à lancer :

| Prompt | Usage |
| --- | --- |
| `design_part(description)` | Concevoir une pièce de bout en bout : hypothèses, plan, construction pas à pas, contrôle final |
| `modify_part(change)` | Modifier la pièce active par ses cotes nommées, avec point de restauration et comparaison de masse |
| `audit_model()` | Auditer le modèle actif sans le modifier : erreurs de reconstruction, esquisses sous-contraintes, références pendantes |
| `batch_export(folder, format)` | Exporter tous les modèles d'un dossier, un fichier à la fois, avec un rapport par fichier |

### À savoir avant de commencer

- **Unités : millimètres et degrés** partout ; masses en grammes, volumes en mm³.
- **Lecture seule par défaut.** `doc_open` ouvre en lecture seule ; demandez explicitement une
  ouverture en écriture pour modifier un fichier existant. Un point de restauration est alors créé
  avant la première modification.
- **Confirmations.** Effacer une fonction, fermer sans enregistrer ou écraser un fichier répond
  d'abord `CONFIRMATION_REQUIRED` : le modèle doit vous présenter l'effet et attendre votre accord.
- **Matériau.** Aucun outil n'assigne de matériau en v1 : assignez-le dans SolidWorks, sinon la
  masse vaut 0.
- **Une mesure prouve, une image oriente.** Demandez un `check_design` (boîte englobante, volume)
  plutôt qu'une capture pour valider une pièce.
- Vous pouvez intervenir dans SolidWorks à tout moment : le serveur relit l'état réel à chaque appel.

## Ce que le serveur expose

**56 outils** en huit familles. Chaque réponse a la même enveloppe
`{ok, data, warnings, error, state, elapsed_ms}`, renvoyée en `structuredContent` et en texte JSON ;
`state` donne l'état réel du document, même après une erreur.

| Famille | Outils |
| --- | --- |
| Session | `sw_connect`, `sw_status` |
| Documents | `doc_list_open`, `doc_new`, `doc_open`, `doc_activate`, `doc_save`, `doc_close`, `doc_info`, `prop_get`, `prop_set`, `checkpoint_create`, `checkpoint_list`, `checkpoint_restore` |
| Esquisse | `sketch_begin`, `sketch_end`, `sketch_add_line`, `sketch_add_rect`, `sketch_add_circle`, `sketch_add_arc`, `sketch_add_polyline`, `sketch_add_slot`, `sketch_add_point`, `sketch_add_relation`, `sketch_add_dimension`, `sketch_inspect` |
| Fonctions | `feature_extrude`, `feature_cut_extrude`, `feature_revolve`, `feature_fillet`, `feature_chamfer`, `feature_hole`, `feature_shell`, `feature_pattern_linear`, `feature_pattern_circular`, `feature_mirror`, `dimension_set`, `variable_set`, `feature_rename`, `feature_suppress`, `feature_delete` |
| Inspection | `inspect_tree`, `inspect_rebuild`, `inspect_mass`, `inspect_dimensions`, `find_faces`, `find_edges`, `inspect_measure`, `inspect_interference`, `check_design`, `view_capture` |
| Export | `export_file` (STEP, STL, IGES, PDF, DXF, PNG) |
| API et connaissances | `api_search`, `api_describe`, `knowledge_get` |
| Échappatoire | `api_call_raw` (désactivé par défaut) |

**7 fiches de connaissance** en ressources `swmcp://knowledge/<sujet>` (ou par l'outil
`knowledge_get`) : `modeling-method`, `sketch-practices`, `features-guide`, `dfm-machining`,
`dfm-sheet-metal`, `materials-and-units`, `error-recipes`. Les valeurs des fiches DFM sont
indicatives et à faire valider par un mécanicien.

**Codes d'erreur** : `NOT_CONNECTED`, `SW_DISCONNECTED`, `SW_BUSY`, `NO_ACTIVE_DOC`,
`WRONG_DOC_TYPE`, `INVALID_INPUT`, `ENTITY_STALE`, `SELECTION_FAILED`, `SKETCH_OPEN`,
`SKETCH_NOT_CLOSED`, `FEATURE_FAILED`, `REBUILD_FAILED`, `PATH_DENIED`, `READ_ONLY_MODE`,
`CONFIRMATION_REQUIRED`, `TIMEOUT`, `UNSUPPORTED_VERSION`, `INTERNAL`. Chaque erreur porte un
`hint` qui dit quoi faire ensuite et un indicateur `retryable`.

## Configuration

Priorité : options de ligne de commande, puis variables d'environnement, puis fichier
`%APPDATA%\swmcp\config.toml`.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `SWMCP_PROFILE` | `design` | Profil d'exposition : `inspect`, `design` ou `full` (voir ci-dessous) |
| `SWMCP_WORKSPACE` | `%USERPROFILE%\Documents\swmcp-workspace` | Dossier de travail : exports, captures, points de restauration |
| `SWMCP_ALLOWED_DIRS` | dossier de travail | Dossiers où le serveur peut lire et écrire, séparés par `;` |
| `SWMCP_LAUNCH_SW` | `0` | Lancer SolidWorks s'il n'est pas ouvert |
| `SWMCP_TIMEOUT_S` | `60` | Délai par appel |
| `SWMCP_LONG_TIMEOUT_S` | `300` | Délai des exports, ouvertures et reconstructions |
| `SWMCP_AUTO_RECONNECT` | `1` | Reconnexion automatique après une perte de connexion |
| `SWMCP_RAW_API` | `0` | Active `api_call_raw` (profil `full` requis) |
| `SWMCP_BACKEND` | `auto` | `com` (SolidWorks) sous Windows, `fake` (simulation) ailleurs |
| `SWMCP_LOG_DIR` | `%LOCALAPPDATA%\swmcp\logs` | Journaux et journal d'audit |
| `SWMCP_LOG_LEVEL` | `INFO` | `DEBUG` trace chaque appel COM |

Options équivalentes de `swmcp serve` : `--profile`, `--workspace`, `--allowed-dir` (répétable),
`--backend`, `--log-level`, `--timeout`.

Exemple de `config.toml` :

```toml
profile = "design"
allowed_dirs = ["C:\\Projets\\CAO", "D:\\Exports"]
timeout_s = 90
```

### Commandes

| Commande | Rôle |
| --- | --- |
| `swmcp serve` | Lance le serveur MCP (c'est le client qui l'appelle) |
| `swmcp doctor [--connect]` | Diagnostic de l'environnement |
| `swmcp selftest [--out fichier.json]` | Séquence de bout en bout et rapport JSON |
| `swmcp index-api` | Construit l'index local de l'API SolidWorks |
| `swmcp install-client <claude-desktop\|claude-code>` | Ajoute le serveur à la configuration du client |
| `swmcp print-config` | Affiche le bloc de configuration à copier |
| `swmcp version` | Versions du serveur, du schéma d'outils et de SolidWorks détecté |

## Sécurité

Le LLM est traité comme une source non fiable ; les contrôles sont dans le serveur.

| Profil | Outils exposés |
| --- | --- |
| `inspect` | Connexion, ouverture en lecture seule, inspection, captures, recherche d'API |
| `design` (défaut) | `inspect` + modélisation, enregistrement, export, points de restauration |
| `full` | `design` + effacement de fonctions, écriture de propriétés, `api_call_raw` si activé |

- Le profil est fixé au démarrage ; les outils interdits sont retirés de la liste, pas seulement refusés.
- Les chemins sont restreints aux dossiers autorisés : liens symboliques résolus, `..`, chemins UNC
  et noms de périphériques rejetés, extensions contrôlées par outil.
- Les actions destructives exigent une confirmation en deux temps (jeton lié à l'outil et à ses
  paramètres, valable 120 s, à usage unique) ou une élicitation MCP quand le client la propose.
- Des points de restauration sont créés avant la première modification d'un document enregistré
  et avant chaque effacement, dans `<dossier de travail>/.swmcp/checkpoints`.
- Chaque appel est inscrit au journal d'audit (`audit.jsonl` dans le dossier des journaux).
- Aucun outil n'exécute de macro ; le texte venu d'un document n'est jamais interprété.

Limite à connaître : le jeton n'empêche pas un modèle de confirmer sans vous demander. La vraie
garantie vient de la validation des appels par le client et des points de restauration.

## Essayer sans SolidWorks

Sur n'importe quelle machine, le backend simulé permet de découvrir les outils :

```bash
swmcp serve --backend fake
```

```bash
swmcp selftest --backend fake
```

Il calcule de vrais volumes pour les extrusions, perçages et révolutions, mais ne reproduit ni le
comportement ni les échecs de SolidWorks.

## Dépannage

| Symptôme | Quoi faire |
| --- | --- |
| Le client ne voit pas le serveur | Vérifier le bloc de configuration, utiliser le chemin absolu de `swmcp.exe`, redémarrer le client |
| `NOT_CONNECTED` | SolidWorks n'est pas ouvert, ou `sw_connect` n'a pas été appelé |
| `SW_BUSY` ou `TIMEOUT` | Un dialogue est ouvert dans SolidWorks : le fermer, puis appeler `sw_status` |
| `PATH_DENIED` | Ajouter le dossier à `SWMCP_ALLOWED_DIRS` |
| `READ_ONLY_MODE` | Rouvrir le document avec `read_only=false`, ou passer au profil `design` |
| `doc_new` échoue | Configurer les modèles par défaut dans SolidWorks, ou fournir `template_path` |
| `INTERNAL` avec un HRESULT | Défaut probable de la couche COM non validée : relancer avec `SWMCP_LOG_LEVEL=DEBUG` et consulter `swmcp.log` |
| `api_search` renvoie `indexed: false` | Lancer `swmcp index-api` |

Les journaux sont dans `%LOCALAPPDATA%\swmcp\logs`. Relancez `swmcp doctor` après toute
modification de l'environnement.

## Développement

```bash
python -m venv .venv
```

Activez l'environnement (`.venv\Scripts\activate` sous Windows, `source .venv/bin/activate` ailleurs), puis :

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

`pytest` joue les tests unitaires, de contrat et le niveau 1 de `sw-bench` sans SolidWorks.
Sur un poste SolidWorks, `pytest -m solidworks` joue les tests d'intégration. Qualité :
`ruff check .` et `mypy`.

- Organisation du code : [docs/architecture.md](docs/architecture.md)
- Écarts par rapport à la spécification et propositions d'outils : [docs/decisions.md](docs/decisions.md)
- Appels COM non vérifiés : [docs/unverified.md](docs/unverified.md)
- Spécification complète : `Spécification — Serveur MCP SolidWorks.md`
- Historique : [CHANGELOG.md](CHANGELOG.md)
