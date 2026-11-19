# Spécification — Serveur MCP SolidWorks

Nov 19, 2026 · @Charlie

## 1. Vision, objectifs et périmètre

Le livrable est un serveur MCP local, déterministe et indépendant du LLM, qui permet à n'importe quel client MCP de piloter l'instance SolidWorks installée sur la machine de l'utilisateur : créer et modifier des pièces, inspecter des modèles, vérifier la géométrie et exporter.

**Principe directeur.** Le serveur ne contient aucun LLM et n'en appelle aucun. Il expose des outils typés, valide les entrées, exécute dans SolidWorks et renvoie un état mesurable. L'intelligence est dans le client ; la fiabilité est dans le serveur.

### Objectifs

1. **Fiabilité avant couverture.** 20 à 30 outils bien testés valent mieux que 200 outils fragiles.
2. **Boucle de feedback complète.** Toute action modifiante renvoie l'état de reconstruction ; des outils de mesure et de capture permettent au LLM de vérifier ce qu'il a construit.
3. **Neutralité.** Protocole MCP standard, transport stdio, aucune dépendance à un client ou à un fournisseur de modèle.
4. **Sûr par défaut.** Mode lecture seule, chemins restreints, confirmations, journal d'audit, points de restauration.
5. **Connaissance embarquée.** Des ressources et des prompts MCP apportent au LLM la méthodologie CAO à la demande.

### Périmètre

| Domaine | Inclus en v1 | Reporté après v1 |
| --- | --- | --- |
| Connexion | Connexion à l'instance ouverte, lancement optionnel, statut | Plusieurs instances simultanées |
| Pièces | Esquisses (ligne, rectangle, cercle, arc, polyligne, fente), relations et cotes, bossage et enlèvement de matière extrudés, révolution, congé, chanfrein, perçage, coque, répétitions linéaire et circulaire, symétrie, édition de cotes nommées | Balayage, lissage, surfaces, tôlerie, soudures |
| Assemblages | Lecture : liste des composants, interférences | Insertion, contraintes (mates), éclatés |
| Mises en plan | Export d'une mise en plan existante (PDF, DXF) | Création de vues, cotation, nomenclature |
| Inspection | Arbre des fonctions, erreurs, masse et volume, boîte englobante, faces et arêtes, cotes, mesures, captures | Épaisseur minimale, dépouille, simulation |
| Export | STEP, STL, IGES, PDF, DXF, PNG | 3MF, formats PDM |
| Données | Propriétés personnalisées, équations et variables globales | Configurations, tables de conception |

**Hors périmètre définitif :** interface graphique, SolidWorks hébergé dans le cloud, multi-utilisateur, API PDM / Electrical / Simulation, redistribution de la documentation ou des binaires Dassault.

**Plateforme.** Windows 10 ou 11 en 64 bits, SolidWorks installé et sous licence sur la même machine, Python 3.11 ou plus. Versions SolidWorks visées : 2023 à 2025 (à confirmer, voir section 14).

## 2. Utilisateurs cibles et scénarios d'usage

Trois profils utilisent le serveur, et chacun impose des exigences différentes : le concepteur veut de la vitesse, l'ingénieur méthodes veut du volume sans surprise, l'intégrateur veut une interface stable.

| Profil | Besoin principal | Exigence pour le serveur |
| --- | --- | --- |
| Concepteur mécanique | Modéliser plus vite des pièces simples à moyennes depuis un client de chat | Retours visuels et mesurés, validation avant action destructrice |
| Ingénieur méthodes / automatisation | Traiter des lots de modèles existants (export, propriétés, audit) | Fiabilité sur gros volumes, reprise sur erreur, journal |
| Intégrateur / développeur | Brancher le serveur à son propre agent ou harnais | Schémas stables, erreurs codées, versionnement |

### Scénarios d'acceptation

Chaque scénario doit pouvoir être joué de bout en bout et sert aussi de test d'intégration.

1. **Création.** « Crée un support en L en acier, 60 × 40 mm, épaisseur 3 mm, avec deux perçages Ø5 à 10 mm des bords. » Résultat attendu : pièce reconstruite sans erreur, boîte englobante et volume conformes à ±1 %, capture iso renvoyée.
2. **Modification paramétrique.** « Passe l'épaisseur à 4 mm et le diamètre des perçages à 6 mm. » Résultat : les cotes nommées changent, la reconstruction réussit, la variation de masse est rapportée.
3. **Audit.** « Ce modèle a des erreurs, dis-moi lesquelles. » Résultat : liste des fonctions en erreur ou en avertissement, esquisses non entièrement contraintes, références pendantes.
4. **Lot.** « Exporte en STEP toutes les pièces du dossier X. » Résultat : un fichier par pièce, rapport de succès et d'échec par fichier, aucun fichier d'origine modifié.
5. **Reprise.** SolidWorks est fermé de force pendant un appel. Résultat : erreur codée et exploitable, puis reconnexion sans redémarrer le serveur.

### Hypothèses d'usage

- Un seul utilisateur, une seule instance SolidWorks, une session MCP active à la fois.
- SolidWorks tourne sur le poste de l'utilisateur, fenêtre visible, et l'utilisateur peut intervenir à tout moment.
- Le LLM est une source non fiable : toute entrée est validée, tout chemin est contrôlé.

## 3. Architecture générale et flux

Tout ce qui vient du LLM traverse la couche MCP, puis les garde-fous, avant d'atteindre SolidWorks par un worker COM unique, seul point de contact avec l'application.

&#91;embedded content: architecture · client, serveur, worker COM, SolidWorks\]

Lecture : de haut en bas, un appel va du client à SolidWorks ; à droite, les connaissances alimentent la couche MCP et les garde-fous écrivent sur le disque local.

### Déroulé d'un appel d'outil

1. Le client envoie la requête `tools/call` (JSON-RPC) ; le SDK MCP la route vers l'outil.
2. L'outil valide les paramètres (pydantic), convertit les unités et vérifie le profil d'exposition.
3. Les garde-fous contrôlent les chemins, exigent une confirmation si l'action est destructive, créent un point de restauration si nécessaire et inscrivent l'appel au journal.
4. L'outil dépose une commande dans la file du worker, qui l'exécute seul dans le thread COM, avec délai et reprises.
5. Le worker rétablit l'état (sélection, options), relit l'état réel de SolidWorks et renvoie des données sérialisables.
6. L'outil formate l'enveloppe de réponse (section 5) et le client la reçoit.

### Principes d'architecture

- **Frontière unique.** Seul le worker voit des objets COM ; les outils manipulent des identifiants et des données.
- **Interface `SwBackend`.** La couche MCP dépend d'une interface, pas de pywin32 : le backend simulé et un éventuel worker C# l'implémentent à l'identique.
- **Pas d'état caché.** Le serveur ne garde que le registre de handles, les jetons de confirmation et la file ; SolidWorks reste la source de vérité.
- **Un client, un SolidWorks.** Une session MCP active et une instance pilotée à la fois (section 1).

## 4. Choix technologiques et structure du dépôt

La v1 se fait en Python 3.11, avec le SDK MCP officiel et pywin32, derrière une interface `SwBackend` qui permettra d'ajouter plus tard un worker C# sans toucher aux outils.

| Décision | Choix | Raison |
| --- | --- | --- |
| Langage | Python 3.11 ou plus | Itération rapide, SDK MCP officiel, bonne lisibilité pour un agent de code |
| SDK MCP | SDK Python officiel (`mcp`, classe `FastMCP`), version épinglée | Standard ; vérifier la révision de spécification MCP supportée à l'installation |
| Accès à SolidWorks | pywin32 en liaison anticipée (early-binding), classes générées depuis les bibliothèques de types (.tlb) de SolidWorks | Signatures et constantes typées, moins de pièges qu'en liaison tardive |
| Validation | pydantic v2 | Les schémas JSON des outils sont générés depuis les modèles |
| Transport | stdio par défaut ; HTTP en streaming sur 127.0.0.1 avec jeton, en option | Clients locaux en priorité |
| Configuration | pydantic-settings : variables d'environnement et fichier TOML | Un seul modèle de configuration validé |
| Journalisation | JSON lines vers fichier et stderr, **jamais stdout** | En stdio, stdout est le canal du protocole : un `print` casse la session |
| Images | Pillow | Redimensionnement et compression des captures |
| Qualité | ruff, mypy en mode strict sur `backend/` et `tools/`, pytest | Garde-fous pour le travail de l'agent de code |
| Distribution | `uv tool install` ou `pipx`, point d'entrée `swmcp` | Installation en une commande |

**Voie de repli.** Si la couverture de l'API ou les performances l'exigent, `com_backend.py` est remplacé par un processus C# (.NET) qui utilise les interops officielles et dialogue par pipe local. L'interface `SwBackend` ne change pas, donc les outils non plus.

### Structure du dépôt

```text
sw-mcp/
  pyproject.toml
  src/swmcp/
    server.py            # application FastMCP, enregistrement des outils
    config.py            # modèle de configuration
    envelope.py          # enveloppe de réponse et codes d'erreur
    safety/              # paths.py, confirm.py, audit.py, checkpoints.py
    backend/
      protocol.py        # interface SwBackend (typing.Protocol)
      com_backend.py     # implémentation réelle (pywin32)
      fake_backend.py    # simulation en mémoire pour les tests
      worker.py          # thread COM unique et file d'appels
      units.py           # conversions mm/deg <-> m/rad
      entities.py        # registre des références d'entités
    tools/               # session, documents, sketch, features, inspect, export, ...
    knowledge/           # ressources Markdown servies au LLM
    prompts/             # prompts MCP (workflows)
    apiindex/            # indexeur des bibliothèques de types
  tests/                 # unit/, integration/, eval/
  docs/
```

### Règles de code

1. Aucune logique SolidWorks dans `tools/` : un outil valide, convertit les unités, appelle le backend et formate l'enveloppe.
2. Tout appel COM passe par le worker de la section 9, jamais depuis une coroutine directement.
3. Les descriptions d'outils sont l'interface réelle avec le LLM : elles disent quand utiliser l'outil, les unités et un exemple court.
4. Aucun accès réseau sortant, aucune télémétrie.
5. Chaque outil a un test unitaire sur le backend simulé avant d'avoir un test sur SolidWorks.

## 5. Catalogue d'outils MCP

La v1 expose une cinquantaine d'outils en huit familles ; le noyau livré aux jalons M0 à M2 en compte une trentaine (colonne Jalon), le reste suit en M3 à M5.

### Conventions communes

1. **Nommage.** `famille_action` en snake\_case : `sketch_add_circle`, `feature_extrude`.
2. **Unités à la frontière.** Millimètres et degrés, paramètres suffixés `_mm` et `_deg`. La conversion vers les mètres et les radians de l'API se fait uniquement dans `units.py`.
3. **Références d'entités.** Les faces, arêtes et segments sont désignés par des handles opaques (`ent:face:12`) délivrés par les outils d'inspection. Chaque handle repose sur une référence persistante de SolidWorks et est résolu à l'usage ; s'il ne l'est plus, l'erreur est `ENTITY_STALE`.
4. **Noms stables.** Après chaque création, le serveur nomme la fonction ou l'esquisse avec le paramètre `name`, ou avec un nom généré `<Type>_<n>`. Aucun outil ne s'appuie sur les noms par défaut de SolidWorks, qui dépendent de la langue de l'installation (voir section 10).
5. **Annotations MCP.** Chaque outil déclare `readOnlyHint`, `destructiveHint` et `idempotentHint` ; les outils de lecture sont marqués `readOnlyHint = true`.
6. **Pagination.** Toute liste de plus de 50 éléments accepte `limit` et `cursor`.
7. **Sortie structurée.** Chaque réponse est renvoyée en `structuredContent` (schéma de sortie déclaré) et en texte JSON équivalent pour les clients qui n'affichent que le texte.

### Enveloppe de réponse

Toutes les réponses ont la même forme. `state` est présent même en cas d'échec, pour que le LLM voie l'état réel après une erreur.

```json
{
  "ok": true,
  "data": { "feature": "Base_1", "volume_mm3": 7200.0 },
  "warnings": [],
  "error": null,
  "state": {
    "doc": "support.sldprt", "doc_type": "part", "dirty": true,
    "rebuild_ok": true, "feature_count": 3, "sketch_open": null
  },
  "elapsed_ms": 412
}
```

En échec : `ok = false` et `error = { "code", "message", "hint", "retryable" }`. Le champ `hint` dit au LLM quoi faire ensuite (par exemple « appeler inspect\_rebuild pour le détail »).

### Familles 1 à 3 : session, documents, esquisse

| Outil | Rôle et paramètres clés | Sortie clé | Jalon |
| --- | --- | --- | --- |
| `sw_connect` | Se connecte à l'instance ouverte. `launch_if_needed` (faux par défaut), `pid` si plusieurs instances | Version, révision, pid, document actif | M0 |
| `sw_status` | État de la session et configuration effective (lecture seule, dossiers autorisés) | État complet | M0 |
| `doc_list_open` | Liste des documents ouverts | Id, chemin, type, modifié ou non | M1 |
| `doc_new` | `type` (part, assembly, drawing), `template_path` (défaut : modèle par défaut de l'utilisateur) | Id et nom du document | M1 |
| `doc_open` | `path`, `read_only` (vrai par défaut) | Id, type, erreurs et avertissements d'ouverture | M1 |
| `doc_activate` | Rend un document actif | Document actif | M1 |
| `doc_save` | Enregistre ; `path` obligatoire pour un document jamais enregistré | Chemin | M1 |
| `doc_close` | `save` ; confirmation si modifications non enregistrées | Fermeture | M3 |
| `doc_info` | Type, chemin, matériau, configuration active, unités du document | Fiche du document | M1 |
| `prop_get`, `prop_set` | Propriétés personnalisées | Valeurs avant et après | M3 |
| `checkpoint_create`, `checkpoint_list`, `checkpoint_restore` | Points de restauration par copie du fichier (section 8) | Id du point | M3 |
| `sketch_begin` | `plane` : `front`, `top`, `right`, handle d'une face plane ou d'un plan ; `name` | Nom, repère de l'esquisse (origine, axes X et Y, normale) dans le repère du modèle | M2 |
| `sketch_end` | Quitte l'esquisse et contrôle son état | Statut : sous-contrainte, entièrement contrainte, sur-contrainte ou échec | M2 |
| `sketch_add_line`, `_rect`, `_circle`, `_arc`, `_polyline`, `_slot`, `_point` | Primitives ; coordonnées en mm dans le repère de l'esquisse ; `_rect` accepte `mode` corner ou center ; `_circle` accepte `radius_mm` ou `diameter_mm` | Handles `ent:sk:<n>` des segments créés | M2 |
| `sketch_add_relation` | `type` : horizontal, vertical, coincident, tangent, equal, parallel, perpendicular, concentric, symmetric, fix, midpoint ; `entities[]` | Relation créée, statut de l'esquisse | M2 |
| `sketch_add_dimension` | `type` : length, diameter, radius, angle, distance ; `entities[]` ; valeur ; `name` | Nom complet de la cote | M2 |
| `sketch_inspect` | `name` | Segments, relations, cotes, statut | M2 |

### Familles 4 à 8 : fonctions, inspection, export, API et échappatoire

| Outil | Rôle et paramètres clés | Sortie clé | Jalon |
| --- | --- | --- | --- |
| `feature_extrude` | `sketch`, `depth_mm`, `end_condition` (blind, mid\_plane, through\_all, up\_to\_next), `direction` (normal, reverse, both), `merge`, `draft_deg`, `name` | Fonction créée, volume avant et après | M2 |
| `feature_cut_extrude` | Mêmes paramètres, pour l'enlèvement de matière | Idem | M2 |
| `feature_revolve` | `sketch`, `axis`, `angle_deg` (360 par défaut), `cut` | Idem | M2 |
| `feature_fillet`, `feature_chamfer` | `edges[]` (handles), `radius_mm` ou `distance_mm` (et `angle_deg` pour le chanfrein) | Idem | M2 |
| `feature_hole` | `face`, `points[]`, `diameter_mm`, `depth_mm` ou `through`. En v1 : esquisse de cercles puis enlèvement extrudé ; l'assistant de perçage (Hole Wizard) vient après | Idem | M2 |
| `feature_shell` | `faces_to_remove[]`, `thickness_mm` | Idem | M5 |
| `feature_pattern_linear`, `feature_pattern_circular`, `feature_mirror` | `features[]`, direction ou axe ou plan, nombre, pas | Idem | M5 |
| `dimension_set` | `full_name` (nom de cote renvoyé à la création), `value` | Ancienne et nouvelle valeur, état de reconstruction | M2 |
| `variable_set` | Variable globale ou équation : `name`, `value`, `unit` | Idem | M5 |
| `feature_rename`, `feature_suppress` | `name`, `new_name` ou `suppressed` | Fonction modifiée | M3 |
| `feature_delete` | `name`, `delete_children` ; destructif, confirmation requise | Fonctions supprimées | M3 |
| `inspect_tree` | Arbre des fonctions : nom, type, supprimée ou non, erreur ou avertissement, parent | Liste paginée | M1 |
| `inspect_rebuild` | Force la reconstruction et renvoie les erreurs et avertissements par fonction | Liste | M1 |
| `inspect_mass` | Volume, surface, masse, centre de gravité, boîte englobante, matériau, densité | Valeurs en mm, mm², mm³, g | M1 |
| `inspect_dimensions` | Toutes les cotes nommées et leur valeur | Liste | M1 |
| `find_faces` | Filtres : `planar`, `normal` (vecteur et tolérance), `extreme` (max\_x, min\_z, etc.), `area_range_mm2`, `near_point_mm` | Handles et descripteurs (aire, centroïde, normale, type de surface) | M2 |
| `find_edges` | Filtres : `linear` ou `circular`, `length_range_mm`, `radius_mm`, `adjacent_to` (face), `near_point_mm` | Handles et descripteurs | M2 |
| `inspect_measure` | `a`, `b` : handles ou points | Distance minimale, angle | M3 |
| `inspect_interference` | Assemblage : paires en interférence | Paires et volumes | M5 |
| `check_design` | `rules[]` : `no_rebuild_errors`, `sketches_fully_defined`, `single_body`, `bbox_within`, `volume_within`, `has_material` avec valeurs attendues | Résultat par règle : attendu, mesuré, réussite | M3 |
| `view_capture` | `view` (iso, front, top, right, back, bottom, left, current), `width_px` (1024 au plus), `zoom_to_fit` | Image PNG | M1 |
| `export_file` | `format` (step, stl, iges, pdf, dxf, png), `path`, options ; écrasement soumis à confirmation | Chemin, taille, durée | M1 |
| `api_search`, `api_describe` | Recherche et description des membres COM depuis les bibliothèques de types locales (section 7) | Signatures | M4 |
| `knowledge_get` | Contenu d'une ressource de connaissance, pour les clients qui ne gèrent que les outils | Texte Markdown | M4 |
| `api_call_raw` | Appel d'une méthode COM nommée sur un objet identifié. **Désactivé par défaut** (`SWMCP_RAW_API=1`), confirmation systématique, journalisé | Retour brut | M5 |

### Codes d'erreur

Le champ `retryable` indique si le même appel peut être rejoué tel quel ; sinon le LLM doit changer ses paramètres ou d'abord inspecter.

| Code | Signification | Rejouable |
| --- | --- | --- |
| `NOT_CONNECTED`, `SW_DISCONNECTED` | Pas de connexion, ou instance perdue | Oui, après `sw_connect` |
| `SW_BUSY` | Appel rejeté ou dialogue modal ouvert | Oui, avec délai |
| `NO_ACTIVE_DOC`, `WRONG_DOC_TYPE` | Aucun document, ou type inadapté à l'outil | Non |
| `INVALID_INPUT` | Validation des paramètres échouée | Non |
| `ENTITY_STALE` | Handle introuvable ou invalidé | Non, relancer `find_faces` ou `find_edges` |
| `SELECTION_FAILED` | L'entité n'a pas pu être sélectionnée | Non |
| `SKETCH_OPEN`, `SKETCH_NOT_CLOSED` | Esquisse encore ouverte, ou contour non fermé | Non |
| `FEATURE_FAILED` | L'API n'a rien créé ; diagnostic joint | Non |
| `REBUILD_FAILED` | Reconstruction en erreur ; liste jointe | Non |
| `PATH_DENIED` | Chemin hors des dossiers autorisés | Non |
| `READ_ONLY_MODE` | Outil modifiant appelé en lecture seule | Non |
| `CONFIRMATION_REQUIRED` | Jeton de confirmation à rejouer (section 8) | Oui, avec le jeton |
| `TIMEOUT` | Délai dépassé ; état à vérifier avant de rejouer | Non |
| `UNSUPPORTED_VERSION` | Fonction absente de la version de SolidWorks | Non |
| `INTERNAL` | Erreur inattendue ; détail dans le journal | Non |

## 6. Boucle de vérification et feedback géométrique

La qualité du résultat dépend davantage de ce que le LLM peut vérifier que de ce qu'il sait : le serveur doit donc rendre chaque action vérifiable, avec des mesures d'abord et des images ensuite.

### Trois niveaux de preuve

| Niveau | Contenu | Coût | Quand l'utiliser |
| --- | --- | --- | --- |
| Léger (`verify = "basic"`, par défaut) | État de reconstruction, nombre de fonctions, nombre de corps, volume et variation de volume | Faible | Après chaque fonction |
| Mesuré (`verify = "full"`, `check_design`, `inspect_mass`) | Boîte englobante, nombre de faces et d'arêtes, règles de conformité | Moyen | À la fin de chaque étape |
| Visuel (`view_capture`) | Image PNG d'une ou plusieurs vues | Élevé en jetons | Validation de fin de tâche, pas à chaque opération |

**Règle de priorité.** Une mesure prouve, une image oriente. Le LLM s'appuie sur les mesures pour conclure qu'une pièce est conforme, et sur la capture pour repérer ce que les chiffres ne montrent pas : orientation, proportions, symétrie.

### Diagnostic quand une opération échoue

Sans diagnostic, le LLM tourne en rond. Sur `FEATURE_FAILED` ou `REBUILD_FAILED`, la réponse joint :

1. la fonction fautive et son type ;
2. le code d'erreur de SolidWorks et son texte, s'il est disponible ;
3. l'état de l'esquisse parente (contour fermé ou non, statut de contrainte) ;
4. un `hint` précis, par exemple « le contour de l'esquisse Profil\_1 n'est pas fermé : vérifier le dernier segment ».

Une reconstruction en erreur ne doit jamais laisser le document dans un état ambigu : le serveur indique si la fonction fautive a été conservée (en erreur) ou annulée.

### Règles de `check_design`

| Règle | Contrôle | Seuil par défaut |
| --- | --- | --- |
| `no_rebuild_errors` | Aucune fonction en erreur ni avertissement bloquant | 0 |
| `sketches_fully_defined` | Toutes les esquisses sont entièrement contraintes | Configurable |
| `single_body` | Un seul corps solide | 1 |
| `bbox_within` | Dimensions de la boîte englobante comparées aux attendues | ±0,1 mm |
| `volume_within` | Volume comparé à l'attendu | ±1 % |
| `has_material` | Un matériau est assigné | Présent |

Le résultat liste pour chaque règle la valeur attendue, la valeur mesurée et la réussite, pour que le LLM corrige la bonne chose.

### Captures

- Fond neutre, ajustement automatique du cadrage, sans éléments d'interface, 1024 px au plus sur le grand côté.
- `view_capture` accepte une liste de vues et peut les assembler en une seule image en grille, pour économiser des jetons.
- L'orientation de la vue de l'utilisateur est restaurée après la capture.

### Budget de réponse

Chaque réponse reste sous environ 2 000 jetons. Les listes longues sont paginées, les noms de fonctions et de cotes sont courts, et aucune image n'est renvoyée sans demande explicite.

## 7. Ressources et prompts MCP : connaissances CAO à la demande

Le serveur apporte la méthodologie CAO au LLM par quatre canaux de poids différent, pour ne charger en contexte que ce dont la tâche a besoin.

| Canal | Contenu | Taille | Chargement |
| --- | --- | --- | --- |
| Champ `instructions` du serveur (initialisation MCP) | Règles de base : unités en mm et degrés, boucle planifier, construire, vérifier, ne jamais deviner un handle, nommer les fonctions, quand attendre une confirmation | 1 500 jetons au plus | Toujours |
| Ressources `swmcp://knowledge/<sujet>` | Fiches de connaissance en Markdown | 500 à 1 500 jetons chacune | À la demande ; outil `knowledge_get` pour les clients qui n'exposent que les outils |
| Prompts MCP | Workflows prêts à lancer | Court | Choisis par l'utilisateur |
| Outils `api_search` et `api_describe` | Signatures de l'API générées depuis les bibliothèques de types locales | Variable | À la demande |

### Fiches de connaissance de la v1

| Fiche | Contenu |
| --- | --- |
| `modeling-method` | Intention de conception ; partir d'une esquisse de base simple et entièrement contrainte ; ordre des fonctions (volumes principaux d'abord, congés et chanfreins en dernier) ; une cote pilote par intention, nommée |
| `sketch-practices` | Contours fermés, pas de segments dupliqués ni chevauchants, relations avant cotes, symétrie par rapport à l'origine, lecture du repère renvoyé par `sketch_begin` plutôt que supposition |
| `features-guide` | Quand choisir extrusion, révolution, congé, coque, répétition ; erreurs classiques (congé plus grand que l'épaisseur, coque après congés) |
| `dfm-machining`, `dfm-3d-printing`, `dfm-sheet-metal` | Règles de conception pour la fabrication : épaisseurs minimales, rayons internes, rapport diamètre et profondeur des perçages, dépouilles. Valeurs à rédiger et faire valider par un mécanicien, avec leurs sources |
| `materials-and-units` | Matériaux courants, densités, conventions d'unités |
| `error-recipes` | Pour chaque code d'erreur du serveur, causes fréquentes et corrections |
| `drawing-conventions` | Cotation, vues, tolérances générales (après v1) |

**Format des fiches.** Markdown en français, avec le terme anglais de l'API entre parenthèses quand il aide. Un en-tête décrit `title`, `when_to_use` et `version`. Chaque fiche tient en moins de 1 500 jetons et commence par la règle à retenir.

### Prompts MCP

1. **`design_part(description)`.** Reformuler le besoin et lister les hypothèses et cotes manquantes ; attendre validation si des informations clés manquent ; planifier les fonctions ; construire étape par étape avec vérification légère après chacune ; terminer par `check_design` et une capture ; rendre un résumé (cotes nommées, hypothèses, écarts).
2. **`modify_part(change)`.** Lire d'abord `inspect_tree` et `inspect_dimensions` ; créer un point de restauration ; modifier par `dimension_set` plutôt que reconstruire ; comparer la masse avant et après.
3. **`audit_model()`.** `inspect_tree`, `inspect_rebuild`, `check_design`, puis un rapport classé par gravité.
4. **`batch_export(folder, format)`.** Lecture seule, un fichier à la fois, rapport de succès et d'échec par fichier.

### Index de l'API

L'outil `api_search` s'appuie sur un index local construit à partir des bibliothèques de types COM installées avec SolidWorks (interfaces, méthodes, paramètres, types, énumérations). Le serveur ne copie ni ne redistribue le texte de la documentation Dassault. La commande `swmcp index-api` construit l'index (SQLite avec recherche plein texte) au premier lancement.

### Gouvernance du contenu

- Chaque fiche porte une version et une date ; un changelog suit les modifications.
- Chaque fiche est chargée par au moins un scénario du jeu d'évaluation (section 12).
- L'effet d'une fiche se mesure par ablation : taux de réussite du jeu d'évaluation avec et sans la fiche. Une fiche sans effet mesurable est retirée ou réécrite.

## 8. Sécurité, garde-fous et validation humaine

Le LLM est une source non fiable et le serveur agit sur la machine de l'utilisateur : la sécurité repose sur des contrôles déterministes dans le serveur, jamais sur le comportement du modèle.

### Menaces et contrôles

| Menace | Contrôle |
| --- | --- |
| Écriture ou écrasement de fichiers hors du projet | Liste blanche de dossiers (`SWMCP_ALLOWED_DIRS`). Chemins normalisés : liens symboliques résolus, `..`, chemins UNC et noms de périphériques rejetés. Extensions autorisées par outil. Sans configuration, seul le dossier de travail du serveur est autorisé |
| Action destructrice (suppression de fonction, fermeture sans enregistrer, écrasement) | Protocole de confirmation en deux temps, voir ci-dessous |
| Modification involontaire d'un fichier existant | `doc_open` en lecture seule par défaut ; l'écriture exige `read_only = false` et un chemin autorisé ; point de restauration automatique avant la première modification |
| Injection de prompt par le contenu des modèles (noms de fichiers, propriétés personnalisées, commentaires, noms de fonctions) | Le serveur n'interprète ni n'exécute jamais un texte venu d'un document. Ces chaînes sont renvoyées dans des champs de données et la fiche `modeling-method` rappelle au LLM de les traiter comme des données |
| Exécution de code arbitraire | Aucun outil d'exécution de macro ou de script. `api_call_raw` est désactivé par défaut et, activé, limité à une liste blanche d'objets et de méthodes |
| Exposition réseau | stdio par défaut. Le mode HTTP écoute sur 127.0.0.1 seulement, exige un jeton porteur aléatoire (comparaison à temps constant) et refuse les en-têtes `Origin` non locaux |
| Emballement (boucles d'appels) | Délai maximal par appel, plafond d'appels par minute, taille maximale des exports |
| Chaîne d'approvisionnement | Dépendances épinglées, `pip-audit` en intégration continue |

### Profils d'exposition

Le profil est fixé au démarrage (`SWMCP_PROFILE`), jamais modifiable par le LLM. Les outils non autorisés sont **retirés de la liste d'outils** plutôt que refusés à l'appel : la surface exposée et le contexte consommé diminuent.

| Profil | Outils exposés | Fichiers |
| --- | --- | --- |
| `inspect` | Connexion, ouverture en lecture seule, inspection, captures, recherche d'API | Aucune écriture, sauf captures dans le dossier de travail |
| `design` (défaut) | `inspect` plus modélisation, export, points de restauration | Écriture dans les dossiers autorisés |
| `full` | `design` plus suppression, propriétés, `api_call_raw` si activé | Idem, avec confirmations |

### Protocole de confirmation

1. Un outil destructif appelé sans jeton répond `CONFIRMATION_REQUIRED` avec une description lisible de l'effet (ce qui sera supprimé ou écrasé) et un `confirm_token` aléatoire, lié à l'outil et à ses paramètres, valable 120 secondes et à usage unique.
2. Le LLM présente l'effet à l'utilisateur et attend son accord, puis rappelle l'outil avec le même jeton.
3. Si le client prend en charge l'élicitation MCP, le serveur interroge directement l'utilisateur à la place du jeton.

**Limite à connaître.** Le jeton n'empêche pas un LLM de confirmer sans demander : la vraie garantie vient de l'élicitation ou de la validation des appels par le client. C'est pourquoi les points de restauration existent : ils rendent l'erreur réversible.

### Points de restauration

- Avant la première modification d'un document enregistré dans la session, et avant chaque opération destructive, le serveur enregistre une copie de l'état courant (enregistrement en mode copie, qui ne change pas le document actif).
- Emplacement : `<dossier de travail>/.swmcp/checkpoints/<document>/<horodatage>-<libellé>`.
- `checkpoint_restore` ouvre la copie comme nouveau document ; il ne remplace jamais l'original sans confirmation.
- Rétention : 20 points par document et 500 Mo au total, configurables.

### Journal d'audit

Une ligne JSON par appel : horodatage, identifiant de session, outil, paramètres (tronqués, sans contenu binaire), code de résultat, durée, document. Les captures ne sont pas journalisées. Fichiers dans `%LOCALAPPDATA%\swmcp\logs`, rotation à 10 Mo sur 5 fichiers. Rien n'est envoyé hors de la machine.

## 9. Fiabilité : COM, threading, erreurs, reprise

Tous les appels COM passent par un thread unique dédié, le serveur tolère l'indisponibilité temporaire de SolidWorks et il revient toujours à un état propre après un outil, qu'il ait réussi ou non.

### Thread COM unique

1. Un thread dédié initialise COM en appartement mono-thread (`pythoncom.CoInitialize`), obtient l'objet application et exécute tous les appels.
2. Les outils (coroutines asyncio) déposent des commandes dans une file ; le worker les exécute une à une et rend le résultat par un futur. Aucun parallélisme : SolidWorks ne le supporte pas.
3. Les objets COM ne quittent jamais le worker. Les outils manipulent uniquement des identifiants et des objets de données sérialisables.
4. À l'arrêt, le worker libère toutes les références puis appelle `CoUninitialize`.

### Délais et progression

- Délai par appel configurable : 60 s par défaut, 300 s pour les exports et les reconstructions lourdes.
- Un appel COM bloqué ne peut pas être interrompu. Au dépassement, le serveur renvoie `TIMEOUT`, passe en état `suspect` et refuse les outils modifiants tant que `sw_status` ne confirme pas que SolidWorks répond.
- Pour les opérations longues, le serveur émet des notifications de progression MCP quand le client fournit un `progressToken`.

### Reprises automatiques

Un nouvel essai n'est fait que lorsque l'appel a été refusé et non exécuté, jamais après un dépassement de délai.

| Situation | Signe | Réaction |
| --- | --- | --- |
| SolidWorks occupé | `RPC_E_CALL_REJECTED` (0x80010001), `RPC_E_SERVERCALL_RETRYLATER` (0x8001010A) | Jusqu'à 5 essais avec délai croissant (100 ms, 200 ms, 400 ms, etc.), puis `SW_BUSY` |
| Instance perdue | `RPC_E_DISCONNECTED` (0x80010108), serveur RPC indisponible (0x800706BA) | Tous les handles sont invalidés, `SW_DISCONNECTED` ; reconnexion automatique à l'appel suivant si `auto_reconnect` est actif (défaut), sinon par `sw_connect` |
| Blocage sans erreur, souvent un dialogue modal | Délai dépassé | `TIMEOUT` avec le conseil de vérifier les dialogues ouverts dans SolidWorks |

### Retour à un état propre

Après chaque outil, dans un bloc `finally` du worker :

1. vider la sélection ;
2. rétablir les options temporairement modifiées (mise à jour graphique, inférence d'esquisse) ;
3. relire l'état réel (document actif, esquisse ouverte) et le renvoyer dans `state`.

Une esquisse ouverte est un état légitime entre `sketch_begin` et `sketch_end`. Tout outil de fonction appelé pendant qu'une esquisse est ouverte renvoie `SKETCH_OPEN` avec le conseil d'appeler `sketch_end`, plutôt que de la fermer en silence. Après une déconnexion, l'état de l'esquisse est relu dans le document.

### Cohérence d'état

- **SolidWorks est la source de vérité.** L'utilisateur peut cliquer à tout moment : le serveur relit l'état au début de chaque outil modifiant.
- Chaque outil modifiant accepte `expected_doc` et refuse d'agir si le document actif est différent, pour ne jamais modifier la mauvaise fenêtre.
- Le registre des handles est vidé à la fermeture du document ; une invalidation est détectée à la résolution et donne `ENTITY_STALE`.
- Un appel actif à la fois ; au plus 10 appels en file, au-delà `SW_BUSY`.

### Performances

- Mise à jour graphique suspendue pendant les longues séquences d'esquisse, une seule reconstruction à la fin de chaque fonction.
- Objectif indicatif : un outil simple répond en moins de 2 s au 95e centile sur une pièce de moins de 50 fonctions, hors reconstruction lourde. `elapsed_ms` figure dans chaque réponse pour le mesurer.

### Observabilité et diagnostic

Les journaux sont structurés avec un identifiant de corrélation par requête MCP ; au niveau DEBUG, chaque appel COM est tracé (méthode, durée, code de retour). La commande `swmcp doctor` vérifie Windows, la version de SolidWorks, l'enregistrement COM, les modèles par défaut, les droits sur les dossiers et la génération des classes pywin32, avec une sortie lisible.

## 10. Pièges de l'API SolidWorks à encoder dans le serveur

Chaque ligne ci-dessous doit devenir soit un helper unique, soit un test d'intégration, pour qu'un piège ne soit résolu qu'une seule fois dans le code.

**Règle pour l'agent de code.** Les noms de méthodes, leurs suffixes numériques et leurs signatures se lisent dans l'API Help de la version ciblée ([help.solidworks.com](https://help.solidworks.com)) ou dans l'index local de `api_search`, jamais de mémoire. Les points marqués « à vérifier » sont des pistes, pas des certitudes.

| Piège | Conséquence | Règle d'implémentation |
| --- | --- | --- |
| L'API travaille en mètres et en radians | Cotes 1 000 fois trop grandes ou petites | Conversion dans `units.py` uniquement, avec tests ; aucun littéral métrique ailleurs |
| Les noms par défaut dépendent de la langue de l'installation (par exemple « Front Plane » en anglais, un nom français sur une installation française ; « Sketch1 » et son équivalent) | La sélection par nom échoue ou agit sur le mauvais objet | Repérer les trois plans de référence par leur type et leur ordre dans l'arbre, pas par leur nom ; renommer chaque objet créé avec un nom du serveur ; manipuler des objets plutôt que des chaînes ; ne jamais analyser un nom par défaut |
| Sélection : l'argument de rappel (callout) de `SelectByID2` doit être un objet nul typé en pywin32 (`VARIANT(pythoncom.VT_DISPATCH, None)`) ; les fonctions lisent la sélection courante et ses marques | Erreurs de type COM, fonction créée sur la mauvaise sélection | Un seul helper `select(entité, marque)` ; sélection vidée avant et après chaque outil ; préférer la sélection directe sur l'objet (à vérifier : `Select2`, `Select4`) |
| Paramètres de sortie par référence (erreurs et avertissements de `OpenDoc6`) | En liaison tardive pywin32, les valeurs de sortie sont perdues ou l'appel échoue | Liaison anticipée générée depuis les bibliothèques de types ; sinon helper dédié avec VARIANT par référence ; test sur un fichier absent et un fichier corrompu |
| Suffixes de version des méthodes (`...2`, `...3`, etc.) | Méthode absente d'une version, ou obsolète | Lire `RevisionNumber` à la connexion et choisir la variante dans une table de capacités par version ; `UNSUPPORTED_VERSION` sinon |
| Échecs silencieux : de nombreuses méthodes de fonction renvoient un objet nul sans message | `FEATURE_FAILED` sans cause exploitable | Sur retour nul, lancer le diagnostic (état de l'esquisse, sélection, reconstruction) et le joindre à l'erreur |
| Repère de l'esquisse différent du repère du modèle selon le plan ou la face | Pièces décalées, pivotées ou perçages mal placés | `sketch_begin` calcule et renvoie origine, axes et normale ; test qui perce à (x, y) puis mesure la position en coordonnées du modèle |
| Inférence et relations automatiques d'esquisse | Contraintes parasites ajoutées pendant la création des segments | Maîtriser l'inférence pendant les créations (option du gestionnaire d'esquisse, à vérifier), la rétablir ensuite, ajouter les relations explicitement |
| Contour non fermé ou segments sécants | Échec de la fonction ou volume inattendu | Contrôle côté serveur avant la fonction : boucles fermées, absence d'intersections |
| Modèle (template) par défaut absent ou non configuré | `doc_new` échoue | Lire le chemin des modèles dans les préférences de SolidWorks ; sinon `INVALID_INPUT` avec le conseil de fournir `template_path` |
| Plusieurs instances de SolidWorks ouvertes | Connexion à la mauvaise instance | `sw_connect(pid)` et liste des instances dans la réponse |
| Gros assemblages : composants allégés | Lecture incomplète des données | Les outils d'inspection signalent `lightweight = true` et ne résolvent un composant que sur demande explicite (`resolve = true`) |
| Masse nulle quand aucun matériau n'est assigné | Volume correct mais masse égale à 0 | `inspect_mass` renvoie `material: null` et un avertissement |

### Constantes et énumérations

Les constantes de SolidWorks (types de documents, conditions de fin, options d'enregistrement) viennent des classes générées depuis les bibliothèques de types, jamais de valeurs numériques recopiées. Un module `constants.py` les regroupe, pour que le reste du code lise des noms et non des entiers.

## 11. Configuration, installation et packaging

L'installation doit tenir en quelques commandes et se terminer par un diagnostic qui dit précisément ce qui manque, car la plupart des échecs d'une première installation viennent de l'environnement et non du code.

### Configuration

Ordre de priorité : options de ligne de commande, puis variables d'environnement, puis fichier `%APPDATA%\swmcp\config.toml`.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `SWMCP_PROFILE` | `design` | `inspect`, `design` ou `full` (section 8) |
| `SWMCP_WORKSPACE` | `%USERPROFILE%\Documents\swmcp-workspace` | Dossier de travail : exports, points de restauration, captures |
| `SWMCP_ALLOWED_DIRS` | Dossier de travail | Dossiers où le serveur peut lire et écrire, séparés par `;` |
| `SWMCP_LAUNCH_SW` | `0` | Lancer SolidWorks s'il n'est pas ouvert |
| `SWMCP_TIMEOUT_S` | `60` | Délai par appel |
| `SWMCP_AUTO_RECONNECT` | `1` | Reconnexion automatique après une perte de connexion |
| `SWMCP_RAW_API` | `0` | Active `api_call_raw` |
| `SWMCP_TRANSPORT` | `stdio` | `stdio` ou `http` |
| `SWMCP_HTTP_PORT`, `SWMCP_HTTP_TOKEN` | `8765`, généré | Mode HTTP seulement, écoute sur 127.0.0.1 |
| `SWMCP_LOG_DIR`, `SWMCP_LOG_LEVEL` | `%LOCALAPPDATA%\swmcp\logs`, `INFO` | Journaux |

### Installation

1. Prérequis : Windows 10 ou 11 en 64 bits, SolidWorks lancé au moins une fois (enregistrement COM), `uv` installé.
2. `uv tool install swmcp` (ou `pipx install swmcp`).
3. `swmcp doctor` : diagnostic complet, à relancer après toute modification de l'environnement.
4. `swmcp index-api` : construit l'index de l'API depuis les bibliothèques de types locales.
5. `swmcp install-client claude-desktop` : ajoute le serveur à la configuration du client, après sauvegarde du fichier existant. Pour les autres clients, `swmcp print-config --client <nom>` affiche le bloc à copier.

Configuration type pour un client en stdio (pour Claude Desktop, le fichier est `%APPDATA%\Claude\claude_desktop_config.json`) :

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

Si le client ne trouve pas la commande, utiliser le chemin absolu de `swmcp.exe`.

### Commandes de la ligne de commande

| Commande | Rôle |
| --- | --- |
| `swmcp serve` | Lance le serveur MCP (point d'entrée des clients) |
| `swmcp doctor` | Vérifie Windows, SolidWorks, COM, modèles par défaut, droits, classes générées |
| `swmcp index-api` | Construit ou met à jour l'index de l'API |
| `swmcp install-client`, `swmcp print-config` | Configuration des clients MCP |
| `swmcp version` | Versions du serveur, du schéma d'outils et de SolidWorks détecté |

### Distribution et versions

- Publication sur PyPI ou un index privé ; en option, un exécutable Windows autonome (PyInstaller) pour les utilisateurs sans Python, signé avec un certificat de signature de code en cas de diffusion externe.
- Versionnement sémantique et `CHANGELOG.md`. Toute modification incompatible d'un schéma d'outil (renommage, suppression de champ) incrémente la version majeure. `sw_status` renvoie `server_version` et `tool_schema_version`.
- Option à évaluer : un format d'extension propre à certains clients de bureau pour une installation en un clic (à vérifier selon le client visé).

### Licence et conformité

Le serveur n'embarque aucun binaire ni aucune documentation de SolidWorks : il utilise l'installation de l'utilisateur et sa licence. Les conditions d'utilisation de SolidWorks et de son API pour un usage automatisé par un tiers ou par une IA doivent être vérifiées avant toute distribution commerciale (question ouverte, section 14).

## 12. Tests et jeu d'évaluation

On ne sait si une modification améliore le serveur qu'en la mesurant : la stratégie combine des tests rapides sans SolidWorks, des tests d'intégration sur le vrai logiciel et un jeu d'évaluation joué avec un LLM réel.

### Niveaux de test

| Niveau | Cible | Environnement | Fréquence |
| --- | --- | --- | --- |
| Unitaire | Validation des entrées, conversions d'unités, enveloppe, confirmations, chemins, registre de handles, logique des outils sur le backend simulé | Windows ou Linux, sans SolidWorks | Chaque commit |
| Contrat | Instantanés des schémas JSON de chaque outil et de l'enveloppe, pour détecter une modification incompatible | Idem | Chaque commit |
| Intégration | Scénarios sur le vrai SolidWorks (marqueur `solidworks`), un document par test, nettoyage systématique | Poste Windows sous licence (runner auto-hébergé) | Chaque PR touchant `backend/` ou `tools/`, et chaque nuit |
| Résilience | Fermeture forcée de SolidWorks pendant un appel, dialogue modal, délai dépassé, handle périmé | Idem | Chaque nuit |
| Compatibilité clients | Session complète avec MCP Inspector, Claude Desktop, Claude Code et un troisième client | Manuel et script | Avant chaque version |
| Évaluation | Jeu de tâches joué avec un LLM réel | Idem | À chaque changement d'outil, de prompt ou de fiche |

**Qualité du code en intégration continue :** ruff, mypy en mode strict, `pip-audit`, couverture d'au moins 85 % sur `tools/` et `safety/` (tests unitaires seuls).

### Backend simulé

`FakeSwBackend` implémente l'interface `SwBackend` avec un modèle en mémoire : documents, esquisses, fonctions, volume calculé pour des primitives simples (boîte, cylindre). Il permet d'écrire tous les outils et leurs tests avant d'avoir accès à SolidWorks et de les exécuter partout. Il ne reproduit ni les comportements ni les échecs du vrai logiciel : les tests d'intégration restent la référence.

### Jeu d'évaluation `sw-bench`

Chaque tâche comprend un énoncé en langage naturel, un fichier YAML d'attendus mesurables et un script de notation indépendant du LLM, qui rouvre le fichier produit dans SolidWorks et mesure. Le jeu compte 30 à 50 tâches réparties en trois niveaux.

| Niveau | Exemples | Critères de réussite | Objectif de départ |
| --- | --- | --- | --- |
| 1. Simple | Plaque avec 4 perçages, cylindre étagé, équerre | Boîte englobante ±0,1 mm, volume ±1 %, position des perçages ±0,1 mm | 90 % ou plus |
| 2. Intermédiaire | Boîtier avec coque et congés, support avec nervures et répétition, pièce de révolution avec gorge | Critères du niveau 1, plus nombre de corps, rayons de congés, épaisseur de paroi | 70 % ou plus |
| 3. Difficile | Pièce à plusieurs plans, symétries, modification d'un modèle existant, audit d'un modèle avec erreurs injectées | Critères précédents, plus erreurs détectées et corrigées | 40 % ou plus |

Les objectifs sont des hypothèses de départ, à fixer définitivement après la première mesure sur le jalon M4.

### Métriques et protocole

1. **Métriques :** taux de réussite (tous critères remplis), nombre moyen d'appels d'outils, nombre d'erreurs par code, temps total, jetons consommés, taux de reprise (erreur suivie d'une correction réussie).
2. **Répétition :** trois essais par tâche pour absorber la variabilité du LLM.
3. **Comparaisons A/B :** avec et sans une fiche de connaissance, avec `verify = "basic"` ou `"full"`, etc.
4. **Analyse des échecs :** les traces complètes (appels et réponses) sont conservées ; chaque échec est classé (outil manquant, message d'erreur peu clair, connaissance manquante, limite du modèle). Cette classification décide de la prochaine amélioration.
5. **Traçabilité :** les résultats sont versionnés dans `tests/eval/results/` avec la version du serveur, le modèle utilisé et la date.
6. **Régression :** une baisse du taux de réussite supérieure à un seuil (5 points proposés) sur un niveau bloque la publication d'une version.

## 13. Plan de réalisation par jalons et critères d'acceptation

Le travail se fait en six jalons ; chacun se termine par une porte, et l'agent de code ne passe au suivant qu'une fois tous les critères de la porte remplis.

&#91;embedded content: jalons M0 à M5 · une porte de validation par jalon\]

Les trois premiers jalons livrent le noyau utilisable ; les trois suivants le rendent sûr, documenté, mesuré et installable. Aucune date n'est fixée ici : elles dépendent des ressources et des réponses aux questions ouvertes de la section 14.

| Jalon | Livrables | Critères d'acceptation (la porte) |
| --- | --- | --- |
| M0. Squelette et connexion | Dépôt, intégration continue, serveur FastMCP en stdio, enveloppe de réponse, worker COM, `sw_connect`, `sw_status`, `swmcp doctor`, backend simulé | `swmcp doctor` réussit sur un poste de test ; `sw_connect` renvoie la version et le document actif ; les outils apparaissent dans MCP Inspector ; tests unitaires verts sans SolidWorks ; aucune écriture sur stdout |
| M1. Documents et inspection | `doc_*`, `inspect_tree`, `inspect_rebuild`, `inspect_mass`, `inspect_dimensions`, `view_capture`, `export_file` | Une pièce existante s'ouvre en lecture seule et fournit arbre, masse, cotes et capture ; l'export STEP réussit ; fichier absent ou corrompu donne une erreur codée |
| M2. Esquisses et fonctions de base | `sketch_*`, `feature_extrude`, `feature_cut_extrude`, `feature_revolve`, `feature_fillet`, `feature_chamfer`, `feature_hole`, `dimension_set`, `find_faces`, `find_edges` | Scénarios 1 et 2 de la section 2 de bout en bout ; les 10 tâches du niveau 1 de `sw-bench` jouées par une séquence d'appels scriptée, sans LLM, avec 100 % de réussite ; tests passants sur une installation non anglophone |
| M3. Sécurité et robustesse | Profils, confirmations, liste blanche de chemins, points de restauration, journal d'audit, délais et reprises, `check_design`, `inspect_measure`, `prop_*`, `feature_rename`, `feature_suppress`, `feature_delete`, `doc_close` | Scénario 5 (reprise après fermeture de SolidWorks) réussi ; tests de traversée de chemins et de liste blanche verts ; aucune action destructive sans jeton ; `check_design` couvre les six règles |
| M4. Fiches et évaluation | Champ `instructions`, ressources, prompts, `knowledge_get`, `api_search`, `api_describe`, `sw-bench` complet | `sw-bench` joué avec un LLM réel ; objectifs par niveau fixés d'après cette première mesure ; effet des fiches mesuré par ablation ; trois clients MCP validés |
| M5. Compléments et packaging | `feature_shell`, répétitions, symétrie, `variable_set`, `inspect_interference`, `api_call_raw` (désactivé), installation, `install-client`, documentation | Installation guidée par `swmcp doctor` sur un poste vierge en une dizaine de minutes (objectif à confirmer) ; scénario 4 (lot) réussi ; `CHANGELOG` et version 1.0 établis |

## 14. Risques, questions ouvertes et consignes pour l'agent de code

Les deux risques qui comptent le plus sont la fragilité des sélections de géométrie et le décalage entre « aucune erreur de reconstruction » et « pièce conforme » ; les parades sont dans les sections 5, 6 et 12.

### Risques

| Risque | Niveau | Parade |
| --- | --- | --- |
| Sélections de faces et d'arêtes fragiles | Élevé | Handles sur références persistantes, `find_faces` et `find_edges` à filtres sémantiques, tests d'intégration dédiés |
| Modèle sans erreur de reconstruction mais non conforme à l'intention | Élevé | Mesures et `check_design`, jeu d'évaluation, validation humaine avant toute fabrication |
| Pièges de pywin32 et de la liaison tardive | Moyen | Liaison anticipée, helpers uniques (section 10), interface `SwBackend`, voie de repli C# |
| Différences entre versions et langues de SolidWorks | Moyen | Table de capacités par version, identification des plans par type, tests sur au moins une installation non anglophone |
| Blocages par dialogues modaux | Moyen | Délais, état `suspect`, `swmcp doctor`, conseils dans les erreurs |
| Poste sous licence nécessaire à l'intégration continue | Moyen | Runner auto-hébergé ; tests d'intégration lancés à la main au début |
| Session de bureau Windows interactive requise par SolidWorks | Moyen | Déploiement sur poste de travail ; une VM ou un serveur exige une session ouverte, à valider avant tout usage hébergé |
| Conditions de licence de SolidWorks pour un usage automatisé | À clarifier | Revue avant toute distribution commerciale |
| Clients MCP qui n'exposent que les outils | Faible | `knowledge_get` et champ `instructions` |

### Questions ouvertes : décisions prises

Les six questions initiales sont tranchées ; ces décisions priment sur les sections 1, 7, 11, 12 et 13 en cas de divergence.

| Question | Décision | Conséquences pour la spécification |
| --- | --- | --- |
| Versions de SolidWorks | Les dernières versions, avec une interface en français | La table de capacités cible les dernières versions, lues par `RevisionNumber` à la connexion. Les noms par défaut étant en français, l'identification des plans par type et le renommage systématique des objets (section 10) sont obligatoires dès M0. |
| Famille de pièces prioritaire | Aucune ; l'impression 3D n'est pas au programme | Jeu d'évaluation généraliste. La fiche `dfm-3d-printing` est retirée de la section 7. |
| Clients MCP | Claude Desktop et Claude Code | Transport stdio uniquement ; le mode HTTP et l'accès distant sont hors v1. `swmcp install-client` cible ces deux clients. La compatibilité est validée sur ces deux clients, et non plus trois. |
| Licence | Aucun problème : usage local | La revue de licence avant distribution n'est plus un point bloquant. |
| Tests sur SolidWorks | Impossibles pour le moment ; la v1 se construit sans test réel | Voir la sous-section suivante. |
| Langue des fiches de connaissance | Français uniquement | Pas de version anglaise ; les termes de l'API restent en anglais entre parenthèses. |

### Conséquences d'une v1 sans test réel

Une v1 vérifiée uniquement sur simulation est un code non validé : la couche COM doit être considérée comme non prouvée tant que `swmcp selftest` n'a pas tourné sur un vrai SolidWorks.

1. Le développement s'appuie sur `FakeSwBackend`, les tests unitaires et les tests de contrat. L'implémentation COM est écrite d'après l'API Help, sans être exécutée.
2. Chaque appel COM non exécuté porte le marqueur `UNVERIFIED` dans le code et figure dans `docs/unverified.md` : méthode, version de l'aide consultée, hypothèse faite.
3. Les tests d'intégration sont écrits mais ignorés par défaut (marqueur `solidworks`) ; ils s'exécuteront tels quels au premier accès à SolidWorks.
4. Une commande `swmcp selftest` joue une séquence automatique (créer une pièce, extruder, mesurer, capturer, exporter, fermer) et produit un rapport JSON des réussites et des échecs, pour corriger vite au premier essai.
5. Les critères de la section 13 qui exigent SolidWorks (`swmcp doctor` sur un poste de test, scénarios de bout en bout, installation sur poste vierge, `sw-bench` avec un LLM réel) sont reportés au premier accès. Pour la v1 sans test réel, une porte est franchie quand ses critères vérifiables sans SolidWorks sont remplis.

### Consignes pour l'agent de code

1. Écrire d'abord l'interface `SwBackend` et le backend simulé, puis les outils, puis l'implémentation COM.
2. Avant d'écrire un appel COM, lire sa signature dans l'API Help ou avec `api_search`, et noter en commentaire la version de la documentation consultée.
3. Créer un helper par piège de la section 10, avec son test.
4. Ne pas ajouter d'outil absent du catalogue sans mise à jour de cette spécification : proposer l'ajout plutôt qu'improviser.
5. Pour chaque outil : modèles pydantic d'entrée et de sortie, description destinée au LLM (quand l'utiliser, unités, exemple), annotations MCP, tests unitaires, et un test d'intégration s'il touche SolidWorks.
6. Ne jamais écrire sur stdout, ne jamais exécuter de contenu provenant d'un document.
7. Livrer chaque jalon avec le code, les tests, le `CHANGELOG`, la mise à jour de `docs/` et la sortie de `swmcp doctor` sur un poste de test.
