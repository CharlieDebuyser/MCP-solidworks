# Architecture

```text
client MCP ──stdio──> server.py (FastMCP)
                        │  tools/registry.py : profil, plafond d'appels, enveloppe, audit
                        ▼
                      tools/*.py  ── valident, appellent `c.sw(lambda b: b.xxx(...))`, formatent
                        │              safety/ : chemins, confirmations, points de restauration
                        ▼
                      runtime.py  ── connexion, expected_doc, lecture seule, point de restauration auto,
                        │              retour à un état propre, relecture de `state`
                        ▼
                      backend/worker.py ── thread COM unique, file bornée, délais
                        ▼
                      SwBackend (backend/protocol.py)
                        ├── fake_backend.py  (simulation, tests)
                        └── com_backend.py   (pywin32, non vérifié)
```

## Règles

- **Frontière unique.** Seul le worker appelle le backend ; les outils ne manipulent que des
  identifiants et des modèles pydantic (`backend/models.py`).
- **Unités.** L'interface `SwBackend` parle en mm, degrés, mm², mm³ et g. La conversion vers les
  mètres et radians de l'API se fait dans `com_backend.py`, par `backend/units.py` exclusivement.
- **Handles.** `backend/entities.py` associe `ent:face:n`, `ent:edge:n`, `ent:sk:n` à une référence
  persistante ; résolution à l'usage, `ENTITY_STALE` sinon. Le registre est vidé à la fermeture du
  document et à la perte de l'instance.
- **État.** Le serveur ne garde que le registre de handles, les jetons de confirmation et la file.
  Après chaque outil, le worker appelle `cleanup()` puis relit `state()`, y compris après un échec.
- **Géométrie partagée.** `backend/geometry.py` (contours fermés, croisements, repères) et
  `backend/composite.py` (`feature_hole`) servent aux deux backends : un piège n'est résolu qu'une fois.
- **stdout.** Rien n'y est écrit hors protocole ; journaux en JSON vers stderr et fichier.

## Ajouter un outil

1. Mettre à jour la spécification (aucun outil hors catalogue).
2. Ajouter la méthode à `SwBackend`, l'implémenter dans `fake_backend.py`, puis `com_backend.py`
   (signature lue dans l'API Help, marqueur `UNVERIFIED`, ligne dans `docs/unverified.md`).
3. Écrire l'outil avec `@tool(...)` : description destinée au LLM (quand l'utiliser, unités,
   exemple), profil, annotations, modèle de sortie.
4. Tests unitaires sur le backend simulé, test d'intégration marqué `solidworks`, puis
   `SWMCP_UPDATE_SNAPSHOTS=1 pytest tests/contract`.
