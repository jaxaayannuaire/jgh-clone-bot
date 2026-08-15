# Changelog

Toutes les évolutions notables du JGH Clone Bot.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/).

## [0.3.0] - 2026-08-15

Notification automatique de fin de déploiement.

### Added
- Suivi de déploiement en tâche de fond (`job_queue`) : après un `/provision`,
  le bot surveille l'avancement et envoie un message de fin sans blocage.
- Message de fin inspiré de l'e-mail d'installation OVH : ✅ succès (URL,
  version Dolibarr, durée, lien `/job`) ou 🔴 échec (raison, lien diagnostic).
- Avertissement ⚠️ si le déploiement dépasse le délai maximal (12 min) sans
  se conclure.
- `CoolifyConnector` : `list_active_deployments()`, `is_deployment_active()`,
  `application_is_running()` pour détecter la fin et le résultat d'un déploiement.

### Changed
- `on_confirm` ne marque plus le job `active` immédiatement : le job reste
  `running` puis passe à `active` (succès) ou `failed` (échec) selon le suivi.
- Dépendance `python-telegram-bot` désormais installée avec l'extra
  `[job-queue]` (requis pour le suivi en tâche de fond).

### Fixed
- Le statut `active` était affiché alors que le déploiement continuait en
  arrière-plan : il reflète maintenant l'état réel de fin de déploiement.

## [0.2.0] - 2026-08-15

Première version déployant de vrais packs depuis le catalogue.

### Added
- Catalogue de packs (`PACKS`) : mappe une clé de pack (ex: `pos`) vers son
  repo `docker-compose`, sa deploy key Coolify et ses métadonnées.
- Commande `/packs` : liste le catalogue avec l'état de configuration de chaque pack.
- Argument `<pack>` dans `/provision <client> <pack> [domaine]`.
- Publication des packs sur GitHub Container Registry (`ghcr.io`, images privées).
- Architecture de pack « Stratégie B » : deux images cuites par pack
  (application + base), déploiement sans bind mount de fichier.
- Injection des documents d'exemple au premier boot via un wrapper d'entrypoint.

### Changed
- `/provision` utilise désormais la deploy key **du pack** (catalogue) au lieu
  d'un repo de test unique en dur.
- `on_confirm` s'appuie sur un contexte de déploiement par confirmation
  (`deploy_ctx`) portant le pack, la deploy key et le nom d'app.
- Le dump SQL de publication est encadré par `SET FOREIGN_KEY_CHECKS=0` pour
  un import fiable (résout l'erreur 1005 errno 150 sur les clés étrangères).
- Nettoyage des références à l'environnement de référence dans le dump
  (chemins absolus neutralisés, données d'exemple conservées).

### Fixed
- Import du dump de pack qui échouait silencieusement via `docker-init.d` de
  Dolibarr (base déjà installée) : le dump est désormais importé par MariaDB
  via `docker-entrypoint-initdb.d`, avant le démarrage de Dolibarr.

### Security
- Deploy keys des packs lues depuis le `.env` (jamais versionnées).
- Images `ghcr.io` privées ; l'hôte de déploiement s'authentifie via
  `docker login ghcr.io`.

## [0.1.0] - 2026-08-11

Version minimale initiale.

### Added
- Flux `/provision` avec dry-run et confirmation inline ✅/❌ (admins).
- `CoolifyConnector` : client de l'API Coolify v1 (création d'application
  compose, déploiement, cycle de vie, healthcheck) validé contre Coolify 4.1.2.
- Traçage DuckDB : tables `clone_jobs` et `pending_actions`, idempotence.
- Commandes `/jobs`, `/job <id>`, `/version`, `/start`.
- Whitelist Telegram (lecture) et liste d'admins (déploiement).
