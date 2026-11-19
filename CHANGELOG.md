# Changelog

Versionnement sémantique. Toute modification incompatible d'un schéma d'outil incrémente la
version majeure et `TOOL_SCHEMA_VERSION`.

## 0.1.0 — 2026-11-19

Première version, construite sans accès à SolidWorks (spec §14). Couvre le code des jalons M0 à
M5 ; les critères de porte qui exigent SolidWorks sont reportés au premier accès.

### Ajouté
- Serveur FastMCP en stdio, enveloppe de réponse commune, 18 codes d'erreur, sortie structurée.
- Interface `SwBackend`, backend simulé paramétrique (`FakeSwBackend`), worker COM à thread unique
  avec file bornée, délais, état « suspect », reprises sur appel rejeté, reconnexion automatique.
- 56 outils : session, documents, propriétés, points de restauration, esquisse, fonctions,
  inspection, `check_design` (six règles), captures, export, index de l'API, `knowledge_get`,
  `api_call_raw` (désactivé par défaut).
- Garde-fous : profils `inspect` / `design` / `full`, liste blanche de chemins, confirmations en
  deux temps et élicitation, points de restauration automatiques, journal d'audit, plafond d'appels.
- 7 fiches de connaissance, champ `instructions`, 4 prompts, index SQLite de l'API.
- Ligne de commande : `serve`, `doctor`, `selftest`, `index-api`, `install-client`, `print-config`,
  `version`.
- Tests unitaires, de contrat (instantanés des schémas) et `sw-bench` niveau 1 scripté (10 tâches).

### Non vérifié
- `backend/com_backend.py`, `apiindex/indexer.py` et les contrôles COM de `doctor.py` n'ont jamais
  été exécutés : voir `docs/unverified.md`.
