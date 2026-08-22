# Changelog

Toutes les évolutions notables du JGH Clone Bot.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/).

## [0.9.2] - 2026-08-21

Ajustements de design `/instances` (retours terrain).

### Changed
- Compteur affiché sur la même ligne que le titre
  (« 📦 Instances — 2 résultat(s) · Page 1/1 ») : supprime l'espacement
  vertical excessif en tête de liste.

### Fixed
- Les boutons 🏠 Accueil / ♻️ Actualiser sur la liste ne créent plus un nouveau
  message quand le contenu est identique : l'erreur Telegram « message is not
  modified » est désormais ignorée silencieusement (édition en place).
## [0.9.1] - 2026-08-21

Améliorations de design de `/instances`.

### Changed
- Séparateurs ramenés à ~42 caractères (au lieu de 60) — plus adapté au mobile.
- Titres d'instances en gras (nom mis en évidence).
- Dates au format français (« 17 août 2026, 02:06 ») via un helper `_date_fr`.
- Texte plus aéré : ligne vide entre chaque bloc (liste et détail).
- Bouton « 📊 Job » du détail : affiche directement le résultat du job
  (rendu partagé avec /job via `_render_job_text`), au lieu de renvoyer vers
  la commande /job.

### Notes
- Champ « déployé par » (WooCommerce, admin Telegram…) prévu pour plus tard.
## [0.9.0] - 2026-08-21

Refonte `/instances` en navigateur liste→détail (style JGH Alert Bot).

### Added
- Module `ui_render.py` : helpers de rendu réutilisables (style Alert Bot) —
  en-têtes avec icône, séparateurs, items numérotés, boutons 2 colonnes
  « n • libellé », pagination (10/page), navigation commune (◀️ Retour,
  🏠 Accueil, ♻️ Actualiser).
- `/instances` refondu : écran LISTE paginé → clic sur une instance → écran
  DÉTAIL (nom, type, domaine, état, date, commande liée) avec actions
  contextuelles (🔗 Ouvrir, 🗑️ Supprimer, 📊 Job).
- Le bouton 🗑️ Supprimer renvoie vers l'assistant `/supprimer` (cohérence).

### Notes
- Même paradigme que JGH Alert Bot, pour une expérience unifiée entre les deux
  bots. `/commandes` sera refondu de la même façon ensuite, puis `/delete`
  (mode direct) sera retiré.
## [0.8.1] - 2026-08-21

Correctif : `/supprimer` plantait au démarrage.

### Fixed
- `cmd_supprimer` injectait les instances brutes (avec des champs datetime
  `online_at`/`created_at`) dans la session wizard ; la sérialisation JSON de
  la session échouait (`Object of type datetime is not JSON serializable`),
  rendant `/supprimer` muet. Correctifs :
  - la commande n'injecte plus que les champs utiles (id, nom, sous-domaine,
    type), sans les dates ;
  - le store sérialise désormais avec `default=str` (robuste à tout type non
    JSON, défense en profondeur).

## [0.8.0] - 2026-08-19

Wizard Phase 2 (suite) — suppression guidée (`/supprimer`).

### Added
- Commande `/supprimer` : assistant guidé de suppression d'une instance.
  Liste les instances actives en boutons (plus sûr que de taper un id).
  Réutilise `_do_delete` (même moteur que `/delete`).
- Confirmation renforcée pour les instances CLIENT : retaper le nom exact
  (Option B). Les instances TEST vont directement au récapitulatif.
- Garde-fous re-vérifiés au moment de l'exécution (déploiement en cours,
  déjà supprimée, pas d'app Coolify).

### Wizard engine — nouvelles capacités (rétrocompatibles)
- `initial_data` : injecter un contexte pré-rempli au démarrage d'un wizard
  (ex. liste d'instances pour les boutons).
- `skip_if(data)` sur une étape : étapes conditionnelles (ex. sauter la
  confirmation par nom pour les tests).
- `on_answer(value, data)` sur une étape choice : enrichir les données après
  un choix (ex. résoudre le type/nom/domaine de l'instance choisie).

### Notes
- `/delete` (mode admin direct) reste disponible en parallèle, le temps que
  `/supprimer` soit éprouvé.
- Modes de suppression avancés prévus pour plus tard (partielle avec archivage
  Drive/OVH, résiliation avec livraison au client) — l'étape « Mode » pourra
  être ajoutée sans refonte.

## [0.7.0] - 2026-08-19

Wizard Phase 2 — déploiement guidé (`/deployer`).

### Added
- Commande `/deployer` : assistant guidé de déploiement d'une instance, basé
  sur le moteur wizard. Étapes : type (client/test) → pack → nom → domaine
  (auto-dérivé, personnalisable) → récapitulatif → VALIDER.
- Le wizard réutilise `_launch_deployment` (même moteur de déploiement que
  `/provision` et `/commandes`) : aucune duplication de logique.
- Seuls les packs déployables (avec deploy key) sont proposés.
- Idempotence conservée (même nom/pack/domaine ne relance pas un doublon).

### Notes
- `/provision` (mode admin expert, arguments en ligne) reste disponible en
  parallèle. `/deployer` est le mode guidé, accessible sans connaître la syntaxe.
- Le récapitulatif du wizard remplace l'ancienne confirmation ✅/❌ de
  `/provision` (une seule confirmation, native au wizard).

## [0.6.0] - 2026-08-17

Moteur d'assistant guidé (wizard) — Phase 1 (fondations).

### Added
- **Moteur conversationnel générique** (`wizard_engine.py`) : exécute des
  assistants multi-étapes définis de façon déclarative (étapes de type choix,
  saisie, confirmation). Une commande = une intention ; paramètres demandés
  progressivement ; boutons quand possible ; valeurs par défaut ; validation
  immédiate ; récapitulatif avant toute écriture ; VALIDER = seule action qui
  exécute ; ANNULER = aucune modification.
- **Persistance des sessions** (`db/wizard_store.py`, table `wizard_sessions`) :
  état en base DuckDB, survit au redémarrage, expiration des sessions
  abandonnées (15 min), anti-double-validation atomique, base d'audit.
- **Runtime Telegram** (`wizard_runtime.py`) : démarrage, reprise/abandon d'une
  session en cours, navigation (retour, modifier un champ, annuler), rendu sur
  un fil de message unique, capture des saisies texte.
- Commande `/demo` : assistant de démonstration (3 étapes) qui valide le moteur
  sans déployer quoi que ce soit.
- Tâche périodique d'expiration des sessions abandonnées.

### Notes
- Phase 1 = fondations + démo. Les fonctionnalités réelles (déploiement,
  suppression, validation de commande) seront migrées vers le wizard en
  Phase 2+. Le mode admin actuel (commandes directes) reste disponible.

## [0.5.3] - 2026-08-17

Correctif : double-clic sur « Déployer » créant des jobs fantômes.

### Fixed
- Cliquer plusieurs fois sur « Déployer » (notamment quand le bot semblait ne
  pas répondre) créait plusieurs jobs pour une même commande. Ajout d'une
  réservation atomique (`try_claim_woo_order`) : une commande ne peut avoir
  qu'un seul job actif à la fois, quels que soient les clics simultanés.
- Le bouton « Déployer » est retiré du message dès le premier clic pris en
  compte, pour éviter les reclics.
- Idempotency_key des déploiements WooCommerce rendue unique par suffixe (uuid
  court) : permet un re-déploiement après échec/suppression sans collision.

## [0.5.2] - 2026-08-17

Correctif : bouton « Déployer » des commandes robuste au redémarrage.

### Fixed
- Le bouton « Déployer » d'une commande ne réagissait plus après un
  redémarrage du bot (les données du bouton étaient en mémoire vive, perdues
  au restart). Le callback relit désormais la commande directement depuis
  WooCommerce à partir de l'order_id (contenu dans le bouton) — plus aucune
  dépendance au cache mémoire.
- Retours du callback plus fiables : repli sur un nouveau message si l'édition
  du message d'origine échoue (message trop ancien), et alertes popup pour les
  refus. Le bouton donne toujours un retour visible.

### Changed
- Idempotence plus explicite : si une commande est déjà provisionnée, le bot
  indique le job et propose /delete pour redéployer.

## [0.5.1] - 2026-08-17

Correctif : faux « déploiement bloqué ».

### Fixed
- Le suivi de déploiement concluait à tort à un échec quand le déploiement
  Coolify était terminé mais que le conteneur démarrait encore (MariaDB importe
  le dump, Dolibarr boote). Ajout d'un **délai de grâce** (`GRACE_MAX_ATTEMPTS`,
  8 tentatives × 15 s = 2 min) : le bot attend que l'application devienne
  `running` avant de conclure. Le 1er déploiement d'un pack (~1min30) n'est plus
  signalé « bloqué » par erreur.

## [0.5.0] - 2026-08-16

Intégration WooCommerce (lecture) : provisionner depuis les commandes.

### Added
- `WooConnector` : client de l'API REST WooCommerce (lecture) — lit les
  commandes, normalise client/produit/meta en objets exploitables.
- Commande `/commandes` : liste les commandes WooCommerce `completed` non encore
  provisionnées, avec un bouton « 🚀 Déployer » par commande.
- Mapping produit WooCommerce → pack (3508→tambali, 3562→pos, 3566→asso,
  3581→pro).
- Cascade de génération du sous-domaine : sous-domaine saisi → société →
  activité → `cmd<numéro>` (dernier recours toujours unique).
- Idempotence par commande : `woo_order_id` sur le job ; une commande ne peut
  être provisionnée qu'une fois (sauf si le job précédent a échoué).
- Fonction de déploiement partagée `_launch_deployment` réutilisée par
  `/provision` et `/commandes` (création + déploiement + suivi de fin).

### Changed
- Schéma DuckDB : colonne `woo_order_id` (migration douce).
- `create_job` et `get_job` gèrent le lien vers la commande WooCommerce.

### Changed (suite)
- Mapping produit -> pack désormais configurable via le `.env`
  (`PACK_<X>_PRODUCT_ID`), centralisé avec le reste de la définition du pack
  (repo, deploy key). Plus de mapping en dur.
- `/commandes` : tri par n° de commande décroissant, date + heure affichées.
- Bouton « Déployer » affiché uniquement pour les packs déployables
  (deploy key présente) — POS seul aujourd'hui. Les packs mappés mais non
  déployables et les produits hors catalogue restent listés avec un
  avertissement (utile pour le nettoyage futur via l'API écriture).

### Notes
- Étape 1 = LECTURE SEULE : le bot lit et provisionne. La validation d'une
  commande (passage à `completed`) et la création de commandes terrain
  (vente hors-ligne) viendront en étape 2 avec une clé API en écriture.
- WooCommerce + Woo Subscriptions restent la source de vérité commerciale.

## [0.4.0] - 2026-08-16

Gestion des instances : suppression et inventaire.

### Added
- Commande `/delete <id>` : résiliation d'une instance (application + données).
  - Instance **test** : double confirmation par boutons.
  - Instance **client** : saisie du nom exact pour confirmer (façon Coolify).
  - Notification de fin « ✅ suppression réussie » (ou 🔴 échec).
- Commande `/instances` : liste les instances déployées avec leur type
  (🧪 test / 👤 client), statut (🟢 en ligne, ⏳ en cours, 🔴 échec) et date
  de mise en ligne.
- Commande `/cancel` : annule une suppression client en attente de saisie.
- Notion de **type d'instance** (`client` par défaut, `test` via le mot-clé
  `test` dans `/provision`) : gouverne le niveau de confirmation à la suppression.
- Horodatage de mise en ligne (`online_at`) et de suppression (`deleted_at`).

### Changed
- `/provision <nom> <pack> [test]` : le mot-clé `test` marque une instance de test.
- Schéma DuckDB étendu (migration douce `ADD COLUMN IF NOT EXISTS`) : colonnes
  `instance_type`, `online_at`, `deleted_at`. Les bases existantes sont migrées
  sans perte au démarrage.

### Security
- Suppression d'une instance client protégée par saisie du nom exact.
- Refus de supprimer un déploiement encore en cours (statut running/pending).

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
