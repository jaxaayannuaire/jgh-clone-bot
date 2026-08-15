# JGH Clone Bot — Étude d'architecture

**Version 1.1 — Août 2026** *(§4.5 ajouté : organisation GitHub tranchée — un repo par pack ; §13.4 clos)*
**Bras exécutant du provisioning conteneurisé de Jaxaay Group : déploiement d'instances Dolibarr sectorisées via Coolify (API REST), catalogue de packs versionnés sur GitHub, migration des documents depuis le serveur master, pilotage Telegram. Successeur du pipeline n8n de clonage live.**

---

## 1. Positionnement et périmètre

### 1.1 Ce qu'est JGH Clone Bot

JGH Clone Bot est le **composant d'exécution du provisioning** de la plateforme JGH Hosting / YessalERP. Il matérialise en Python l'`InstanceOrchestrator` décrit dans `YessalERP_Projet.md`, piloté par Telegram en phase 1, appelable par WooCommerce/Laravel plus tard.

Il fait partie d'un ensemble de trois briques qui partagent la même infrastructure et les mêmes patterns :

| Brique | Rôle | Posture |
|---|---|---|
| **JGH Alert Bot** (v1.18.0, opérationnel) | Supervision, échéances, alertes | **Lecture seule** |
| **JGH Clone Bot** (ce document) | Provisioning, clonage, migration, restauration | **Écrit et exécute** |
| **JG Hosting / YessalERP** | Couche commerciale + control plane Laravel | Orchestration |

### 1.2 Décision structurante : Coolify est le cœur

La décision fondatrice, tranchée en amont de ce document : **Coolify (conteneurs Docker) est le paradigme nominal du provisioning ; le clonage live ISPConfig→CloudPanel n'est plus qu'un outil de migration one-shot.**

Cela découle directement de `comparaison-cloudpanel-vs-coolify-yessalerp.md` et `JG_Hosting_Synthese.md` : l'image Docker officielle Dolibarr se configure par variables d'environnement (`DOLI_DB_*`, `DOLI_URL_ROOT`, `DOLI_ENABLE_MODULES`), ce qui **supprime l'étape `sed -i` sur `conf.php`** — la partie la plus fragile du pipeline historique.

### 1.3 Les trois modes du bot

| Mode | Usage | Paradigme | Priorité |
|---|---|---|---|
| **provision** | Nouveau client → nouvelle instance | Coolify + image + code monté + seed SQL | Nominal (Phases 1–2) |
| **migrate** | Client legacy ISPConfig/CloudPanel → conteneur | Dump live + import dans conteneur neuf | One-shot (Phase 4) |
| **restore** | Restauration d'instance depuis sauvegarde | Réhydratation base + volume documents | Phase 5 |

> Le mode `provision` est le cœur. Le mode `migrate` réutilise la logique de l'ancien pipeline n8n **uniquement comme voie de sortie du legacy**, jamais comme voie d'entrée.

### 1.4 Ce que JGH Clone Bot n'est PAS

- **Pas le module de publication de packs.** Ce module vit dans Dolibarr (composant PHP autonome, §7). Le bot **consomme** le catalogue de versions, il ne le produit pas.
- **Pas le control plane commercial.** WooCommerce/Laravel restent maîtres de l'état commercial. Le bot exécute des ordres de provisioning.
- **Pas un orchestrateur généraliste dès le départ.** On commence par Dolibarr (cas le plus mûr), on généralise après.

---

## 2. Ce qui est hérité de JGH Alert Bot

JGH Clone Bot réutilise le socle éprouvé plutôt que de réinventer.

### Patterns réutilisés tels quels
- **Contrôle-plan / données-plan** : le bot orchestre, Coolify exécute.
- **Connexion DuckDB unique stable** + schéma versionné.
- **Sentinelles `JGH_JSON_START…END`** pour tout parsing de stdout SSH (mode migrate surtout).
- **File `pending_*` + boutons inline Telegram ✅/❌** : un clonage est une action destructive/coûteuse, même circuit de confirmation.
- **Connecteurs par compte OVH** (multi-comptes, CK par compte).
- **Secrets hors repo**, systemd, déploiement par git, français, fuseau Africa/Dakar.

### Intégrations réutilisées (clients déjà codés et testés)
- **Client Dolibarr** (`dolibarr_client.py`) : REST avec allowlist/denylist — pour créer projet/tiers/contrat et injecter les données client après boot.
- **Client OVH** (`ovh_client.py`) : provisionner de nouveaux services (domaines, VPS) le jour venu.
- **Client Telegram** : interface de commande et confirmation.
- **Miroirs Google Sheets/Agenda** : pour journaliser les opérations de clonage.

### Le changement de posture majeur
**Alert Bot est lecture seule ; Clone Bot écrit et exécute sur des serveurs de production.** C'est le sujet de sécurité central (§8). Là où Alert Bot utilise une clé SSH à commande forcée en lecture seule, Clone Bot agit par **jetons Coolify scopés** (`write`/`deploy`) — une surface d'attaque réduite et versionnée, pas un SSH root large.

---

## 3. Architecture générale

```
┌────────────────────────────────────────────────────────────────┐
│                    VPS D'EXPLOITATION (bot)                      │
│                    57.131.27.63 — CloudPanel                    │
│                                                                 │
│  ┌──────────┐   /provision      ┌────────────────────────────┐ │
│  │ Telegram │◄──/migrate───────►│    JGH Clone Bot (py)       │ │
│  │  (admin) │   /packs /jobs    │    python-telegram-bot      │ │
│  └──────────┘   ✅/❌ confirm    │    + JobQueue               │ │
│                                 │  ┌──────────────────────┐   │ │
│  ┌──────────┐                   │  │  DuckDB clone.duckdb │   │ │
│  │ WooCommerce│──webhook──(P.6)─►│  │  clone_jobs          │   │ │
│  │ /Laravel   │  order.completed │  │  pack_versions       │   │ │
│  └──────────┘                   │  └──────────────────────┘   │ │
│                                 │  ┌──────────────────────┐   │ │
│                                 │  │  Connecteurs         │   │ │
│                                 │  │  ├─ CoolifyConnector │   │ │
│                                 │  │  ├─ GitHubConnector  │   │ │
│                                 │  │  ├─ DocumentsMover   │   │ │
│                                 │  │  ├─ DolibarrClient   │   │ │
│                                 │  │  └─ (SSH migrate)    │   │ │
│                                 │  └──┬────────┬──────┬───┘   │ │
│                                 └─────┼────────┼──────┼───────┘ │
└───────────────────────────────────────┼────────┼──────┼─────────┘
              API REST /api/v1  ▲        │        │      │
                                │   GitHub API    │   SSH (documents)
                    ┌───────────┴──────┐ │        │      │
                    │ VPS 2 — Coolify  │ │  ┌─────▼────┐ │
                    │ (control plane)  │ │  │ GitHub   │ │
                    │  57.x.x.x        │ │  │ (code +  │ │
                    └────────┬─────────┘ │  │  dump)   │ │
                        SSH  │ (interne  │  └──────────┘ │
                             │  Coolify) │               │
                    ┌────────▼─────────┐ │        ┌──────▼────────┐
                    │ VPS TEST/DÉPLOI  │◄┘        │ SERVEUR MASTER│
                    │ flotte Dolibarr  │◄─────────│ documents/    │
                    │ conteneurs Docker│  docs    │ zip par version│
                    └──────────────────┘          └───────────────┘
```

### Principes directeurs

- **Le bot ne parle jamais SSH aux conteneurs.** Il pilote Coolify par API REST ; Coolify gère le SSH vers ses cibles en interne.
- **GitHub est le registre de code + structure.** Le code Dolibarr (sans documents) et le dump SQL de seed vivent dans GitHub, versionnés par tags/releases.
- **Le serveur master détient les documents** (trop lourds pour git), archivés en zip par version, migrés au déploiement.
- **DuckDB est la source de vérité locale** de l'état des jobs et du catalogue de packs.
- **Toute action destructive/coûteuse passe par confirmation** (file `pending`, boutons Telegram).

---

## 4. Le modèle de packs (rappel figé)

### 4.1 Trois artefacts par version de pack

| Artefact | Contenu | Où | Poids type |
|---|---|---|---|
| **Code** | Fichiers Dolibarr + `custom/`, **sans** documents | GitHub (tag/release) | ~150 Mo |
| **Structure + seed** | Dump SQL | GitHub (avec le code) | ~30 Mo |
| **Documents d'exemple** | Fichiers d'exemple (noms/sociétés **fictifs**) | Serveur master, zip par version | ~70 Mo |

> **Règle validée** : les documents de référence ne contiennent que des données d'exemple fictives — jamais de données d'un vrai client. Le zip sert au **seed initial** d'un nouveau clone et à la **restauration du pack de référence**, distinct du backup des données client (volume `documents` par tenant).

### 4.2 Les packs et leur cycle de vie

Cinq packs au catalogue : **Tambali, Asso, Pro, Immo, POS**. Démarrage effectif avec **POS** (`pos.yessal.com`) et **Pro** (`packpro.yessal.com`).

Cadence : **2 versions par pack et par an**. Seules les **2 dernières versions** de chaque pack sont actives pour le clonage ; les plus anciennes sont archivées (Google Drive / serveur d'archive) puis supprimées du VPS master et de GitHub.

```
Ex : Pack TBLI v1.0.0 (janv. 2027) ── active
     Pack TBLI v2.0.0 (juin 2027)  ── active
     → au lancement de v3.0.0, v1.0.0 passe "archived" puis est purgée
```

### 4.3 Stratégie Docker : image par version de Dolibarr, pas par pack

La décision d'architecture la plus importante pour la maîtrise des coûts :

> **Le nombre d'images Docker ≈ le nombre de versions de Dolibarr encore en production (2–3 vivantes), jamais le nombre de variantes de packs (20).**

Ce qui distingue un pack d'un autre (modules, code `custom/`, seed) est monté **par-dessus** une image Dolibarr commune, pas cuit dans une image dédiée.

| Ce qui varie | Vit dans | Mécanisme |
|---|---|---|
| Binaire Dolibarr + PHP | **Image Docker** | `jgh/dolibarr:<version-doli>` (2–3 vivantes) |
| Modules activés | Variable d'env | `DOLI_ENABLE_MODULES` |
| Code maison `custom/` | **Volume monté** | tag GitHub cloné sur l'hôte |
| Données de seed | Import au déploiement | dump SQL du tag |
| Documents d'exemple | Archive montée | zip master → volume tenant |

**Conséquence opérationnelle décisive** : un patch de sécurité Dolibarr = reconstruire **une image**, dont héritent les 20 combinaisons au prochain déploiement. Avec « une image par variante », ce serait un chantier de 20 rebuilds.

### 4.4 Montage du code : volume (option retenue)

Le code du pack est **monté en volume** depuis le tag GitHub cloné sur l'hôte (pas copié au boot, pas buildé dans l'image) :

```
/data/packs/pos-v1.0.0/custom     ← git checkout du tag
        │ monté READ-ONLY ▼
Conteneur Dolibarr (image jgh/dolibarr:21)
   /var/www/html/custom   ←── volume code pack (READ-ONLY)
   /var/www/documents     ←── volume documents tenant (READ-WRITE)
   DOLI_*                 ←── envs/bulk (remplace le sed)
```

- `custom/` du pack en **lecture seule** (c'est du code, il ne doit pas muter en prod).
- `documents/` du tenant en **lecture-écriture** (données du client).
- **Ne jamais mélanger les deux volumes.**

> **Exception** : un pack exigeant une extension PHP exotique ou une modification du cœur Dolibarr bascule — lui seul — en image dédiée (build multi-stage). Pour du Dolibarr sectorisé standard, le volume suffit.

> **À valider au premier déploiement test** : le point de montage exact de `custom/` dépend de l'arborescence de l'image Dolibarr officielle réellement utilisée. Réglage de chemin, pas changement de stratégie.

### 4.5 Organisation GitHub : un repo par pack (décidé)

**Décision : un repo GitHub privé par pack**, pas un repo commun à tags préfixés. Facteurs décisifs :

- **Poids** — le code d'un pack pèse ~180 Mo/version et l'historique git ne se purge pas quand on supprime un tag (le blob reste dans l'historique). Un repo commun accumulerait 5 packs × versions et deviendrait un dépôt de plusieurs Go, lent à cloner. Cinq repos bornés restent sains.
- **Cycle de vie** — la règle « archiver puis purger les vieilles versions » s'applique proprement repo par repo, jusqu'à l'abandon complet d'un pack (on archive/supprime le repo entier).
- **Isolation d'accès** — une deploy key Coolify par repo n'ouvre l'accès qu'à un seul pack, jamais à tout le catalogue (privilège minimal, cohérent avec les jetons scopés et l'allowlist Dolibarr).
- **Lisibilité** — releases et changelogs isolés par pack, ce qui sert directement `/pack_info` et la présentation catalogue WooCommerce.

> Le partage de code entre packs n'est **pas** un besoin ici : le socle Dolibarr commun vit dans l'**image Docker**, pas dans les repos ; chaque pack sectoriel n'a que son propre code métier. L'argument monorepo (mutualiser du code commun) tombe donc.

**Convention de nommage des repos** (figée) :

```
jaxaayannuaire/jgh-pack-pos
jaxaayannuaire/jgh-pack-pro
jaxaayannuaire/jgh-pack-tambali
jaxaayannuaire/jgh-pack-asso
jaxaayannuaire/jgh-pack-immo
```

**Tags par repo** : semver simple, sans préfixe de pack (le repo *est* le pack) — `v1.0.0`, `v2.0.0`, `v3.0.0`…

**Une deploy key Coolify par repo**, générée au moment de brancher chaque pack sur Coolify.

Le `GitHubConnector` lit une **config statique** `pack → repo` (pas de découverte automatique : 5 packs connus, pas 500) :

```python
PACKS = {
    "pos":     "jaxaayannuaire/jgh-pack-pos",
    "pro":     "jaxaayannuaire/jgh-pack-pro",
    "tambali": "jaxaayannuaire/jgh-pack-tambali",
    "asso":    "jaxaayannuaire/jgh-pack-asso",
    "immo":    "jaxaayannuaire/jgh-pack-immo",
}
```

`catalog_sync` boucle sur ces repos, lit les releases GitHub de chacun et alimente `pack_versions` (le champ `github_repo` stocke le repo par ligne — voir §9).

---

## 5. L'API Coolify — routes utilisées

Coolify **4.1.2**. API versionnée sous `/api/v1`, authentification par jeton Laravel Sanctum, permissions granulaires par jeton (`read`/`write`/`deploy`). Le bot utilise un jeton scopé `write`+`deploy` — **jamais `root`**.

| Besoin | Route | Permission |
|---|---|---|
| Créer l'app (repo privé, deploy key) | `POST /api/v1/applications/private-deploy-key` | write |
| Créer l'app (image pré-construite) | `POST /api/v1/applications/dockerimage` | write |
| Créer la base MariaDB du tenant | endpoints `databases` (section DB de l'API) | write |
| Injecter les variables `DOLI_*` en masse | `PATCH /api/v1/applications/{uuid}/envs/bulk` | write |
| Déclencher le déploiement | `POST /api/v1/applications/{uuid}/start` | deploy |
| Arrêter / redémarrer | `POST /api/v1/applications/{uuid}/stop` \| `/restart` | deploy |
| Détail / suivi d'une app | `GET /api/v1/applications/{uuid}` | read |
| Supprimer (résiliation) | `DELETE /api/v1/applications/{uuid}` | write |

Paramètres utiles à la création :
- **`instant_deploy`** (défaut `false`) : crée **et** déploie en un appel.
- **`force_domain_override`** (défaut `false`) : renvoie **HTTP 409** si le sous-domaine est déjà pris → le bot traite le 409 comme « sous-domaine indisponible », jamais d'écrasement silencieux.
- **`autogenerate_domain`** : à laisser `false` puisqu'on impose `client.yessalerp.com`.

> **Deux pièges connus, à gérer dès le code :**
> 1. `removeSensitiveData` **ampute** certains champs de réponse (`dockerfile`, `docker_compose_raw`, secrets webhook…) si le jeton n'a pas la lecture sensible — ne pas confondre avec un bug.
> 2. La doc Coolify a des **écarts spec/réalité** connus (ex. champ `environments` absent de la réponse `projects` alors que documenté). **Valider chaque payload contre l'instance 4.1.2 réelle** au moment de coder, sans se fier aveuglément à la spec.

---

## 6. Flux de bout en bout — mode provision

```
/provision pos v1.0.0 <client> <sous-domaine>   (Telegram, admin)
        ↓
[1] Le bot calcule le plan (nom base, sous-domaine, tag, image Doli)
    → insère un clone_job en status='pending', dry_run=TRUE
        ↓
[2] Message Telegram récapitulatif + boutons ✅ Confirmer / ❌ Ignorer
        ↓ (✅)
[3] git checkout tag v1.0.0 depuis GitHub (code + dump SQL) → /data/packs/pos-v1.0.0
        ↓
[4] Coolify : création base + user MariaDB dédiés au tenant
        ↓
[5] Coolify : POST applications/... (image jgh/dolibarr:21, volume custom/, ports)
        ↓
[6] PATCH envs/bulk : DOLI_DB_*, DOLI_URL_ROOT, DOLI_ENABLE_MODULES, admin initial
        ↓
[7] POST {uuid}/start (ou instant_deploy=true) → déploiement conteneur
        ↓
[8] Import du dump SQL de seed dans la base du tenant
        ↓
[9] Migration documents : master → VPS déploiement (zip de la version, extraction volume)
        ↓
[10] SSL : Traefik obtient le certificat Let's Encrypt (sous-domaine)
        ↓
[11] Injection client via API REST Dolibarr (raison sociale, XOF, admin client)
        ↓
[12] Vérif santé (l'instance répond) → clone_job status='active'
        ↓
    Notification Telegram + journalisation Sheets/Agenda
```

### Ordre de grandeur des temps (pack POS ~250 Mo)

| Régime | Durée |
|---|---|
| **1er déploiement d'un pack** (image Docker à télécharger) | ≈ 2 min 30 – 6 min |
| **Déploiements suivants** (image en cache local) | ≈ 1 min 15 – 3 min 30 |

Le pull de l'image est le seul gros poste, **ponctuel** et amorti sur toute la flotte grâce à l'image unique. Régime nominal cohérent avec l'estimation « ~2–4 min » de `JG_Hosting_Synthese.md`.

### Contrat WooCommerce (Phase 6, à garder en tête dès maintenant)
Quand le déclencheur deviendra commercial : webhook sur **`order.completed`**, jamais `order.created` (le mobile money passe souvent par `on-hold`). Idempotence par `woo_order_id`.

---

## 7. Le module Dolibarr de publication (composant distinct)

Le « module de sauvegarde pour le clonage » est un **module Dolibarr maison (PHP)**, installé sur les environnements de référence (`pos.yessal.com`, `packpro.yessal.com`). **Il ne fait pas partie du bot** — il alimente le catalogue que le bot consomme. Cette séparation respecte le contrôle-plan / données-plan : la publication vit là où est la donnée.

### Rôle
1. Écran admin « Publier une version » : numéro sémantique, changelog (fonctionnalités / fix / nouveautés), tags.
2. Après validation admin :
   - **Push code + dump SQL vers le repo GitHub dédié du pack** (`jgh-pack-<pack>`, release taguée `vX.Y.Z` via l'API GitHub — voir §4.5).
   - **Déclenche l'archivage zip des documents** de la version sur le serveur master (`documents_<pack>_<version>.zip`).
3. Enregistre les métadonnées de version que le bot/WooCommerce liront pour présenter le catalogue.

### Frontière avec le bot
- **Le module publie** (produit les artefacts).
- **Le bot consomme** : il lit les releases GitHub pour alimenter sa table `pack_versions`, et propose au clonage les versions actives.

---

## 8. Sécurité des exécutions

Clone Bot écrit et exécute sur la production : c'est le sujet nouveau par rapport à Alert Bot.

| Principe | Mise en œuvre |
|---|---|
| **Confirmation systématique** | Toute action destructive/coûteuse passe par la file `pending` + boutons ✅/❌ (admins only) |
| **Dry-run par défaut** | Le job calcule et affiche le plan complet (base, sous-domaine, tag, image) **avant** toute écriture |
| **Idempotence** | Clé de job unique (`idempotency_key`) : rejouer un clonage échoué ne double rien |
| **Privilège minimal** | Jeton Coolify scopé `write`+`deploy`, jamais `root` ; pas de SSH root large |
| **Trace intégrale** | stdout/stderr de chaque job conservés (`clone_jobs.stdout_log`), comme `provisioning_jobs` |
| **Rollback** | Job échoué → `status='failed'` + trace exacte, rejouable sur sa clé d'idempotence |
| **Denylist Dolibarr** | Reprise de l'allowlist/denylist du client existant (secrets `loginovh`/`loginrootvps`/`rootpassword` jamais lus/écrits) |
| **Secrets** | `.env` chmod 600, `keys/` gitignored ; jetons Coolify/GitHub hors repo |
| **Conflits de domaine** | `force_domain_override=false` → 409 traité explicitement, jamais d'écrasement |

> **Règle non négociable héritée** : une seule image pour toute la flotte, modules par variable d'env, code maison en volume `custom/`. Un client = une configuration, **jamais** une image dédiée.

---

## 9. Modèle de données DuckDB

```sql
-- Catalogue des versions de packs (miroir des releases GitHub)
CREATE TABLE IF NOT EXISTS pack_versions (
    id INTEGER PRIMARY KEY,
    pack VARCHAR NOT NULL,              -- 'pos','pro','tambali','asso','immo'
    version VARCHAR NOT NULL,           -- '1.0.0' (semver)
    dolibarr_image VARCHAR NOT NULL,    -- 'jgh/dolibarr:21'
    github_repo VARCHAR,                -- repo GitHub du code (jgh-pack-<pack>, §4.5)
    github_tag VARCHAR,                 -- tag de la release (vX.Y.Z, semver simple)
    documents_zip VARCHAR,             -- nom de l'archive sur le master
    modules VARCHAR,                    -- liste DOLI_ENABLE_MODULES
    changelog TEXT,                     -- fonctionnalités / fix / nouveautés
    status VARCHAR DEFAULT 'active',    -- 'active' | 'archived' | 'purged'
    published_at TIMESTAMP,
    UNIQUE (pack, version)
);

-- Jobs de provisioning / migration / restauration
CREATE TABLE IF NOT EXISTS clone_jobs (
    id INTEGER PRIMARY KEY,
    job_type VARCHAR NOT NULL,          -- 'provision' | 'migrate' | 'restore'
    idempotency_key VARCHAR UNIQUE,     -- rejeu sans doublon (woo_order_id en P.6)
    client_name VARCHAR,
    pack VARCHAR,
    pack_version VARCHAR,
    subdomain VARCHAR,
    dolibarr_socid INTEGER,             -- tiers Dolibarr lié (injection client)
    coolify_app_uuid VARCHAR,           -- UUID de l'app Coolify créée
    coolify_db_uuid VARCHAR,            -- UUID de la base MariaDB tenant
    db_name VARCHAR,
    status VARCHAR DEFAULT 'pending',   -- pending|confirmed|running|active|failed
    dry_run BOOLEAN DEFAULT TRUE,
    stdout_log TEXT,                    -- trace exacte conservée
    error_message TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp,
    confirmed_at TIMESTAMP,
    resolved_at TIMESTAMP
);

-- File de confirmation (même pattern que pending_dolibarr_writes d'Alert Bot)
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES clone_jobs(id),
    action_type VARCHAR,                -- 'provision' | 'delete' | 'migrate' | ...
    summary VARCHAR,                    -- récap montré sur Telegram
    status VARCHAR DEFAULT 'pending',   -- pending|confirmed|rejected|expired
    created_at TIMESTAMP DEFAULT current_timestamp,
    resolved_at TIMESTAMP
);

-- Instances déployées (état courant de la flotte)
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY,
    client_name VARCHAR,
    pack VARCHAR,
    pack_version VARCHAR,
    subdomain VARCHAR UNIQUE,
    coolify_app_uuid VARCHAR,
    dolibarr_socid INTEGER,
    status VARCHAR DEFAULT 'active',    -- active|suspended|terminated
    deployed_at TIMESTAMP,
    expires_on DATE                     -- repris de Woo/Dolibarr (jonction Alert Bot)
);
```

---

## 10. Arborescence du projet (proposition)

```
jgh-clone-bot/
├── bot.py                       # point d'entrée (JobQueue + handlers Telegram)
├── db/
│   ├── schema.sql               # DDL (§9)
│   └── duckdb_client.py         # connexion unique stable (pattern Alert Bot)
├── connectors/
│   ├── coolify_connector.py     # API REST /api/v1 (create/deploy/env/delete)
│   ├── github_connector.py      # releases, tags, checkout du code+dump
│   └── documents_mover.py       # transport zip master → VPS déploiement (SSH/SFTP)
├── integrations/
│   ├── dolibarr_client.py       # RÉUTILISÉ d'Alert Bot (injection client)
│   ├── sheets_sync.py           # RÉUTILISÉ (journal des opérations)
│   └── calendar_sync.py         # RÉUTILISÉ (échéances)
├── modes/
│   ├── provision.py             # mode nominal (Coolify)
│   ├── migrate.py               # legacy → conteneur (SSH + sentinelles)
│   └── restore.py               # restauration depuis backup
├── jobs/
│   ├── job_runner.py            # exécution étape par étape + trace stdout
│   └── catalog_sync.py          # GitHub releases → pack_versions
├── core/
│   ├── confirmation.py          # file pending_actions + boutons inline
│   └── dry_run.py               # calcul et affichage du plan
├── deploy/
│   └── install.md               # systemd, .env (pattern Alert Bot)
├── tests/
├── .env.example
└── requirements.txt
```

---

## 11. Commandes Telegram (phase 1)

| Commande | Rôle |
|---|---|
| `/packs` | Lister les packs et versions actives (depuis `pack_versions`) |
| `/pack_info <pack> <version>` | Détail d'une version (changelog, modules, image Doli) |
| `/provision <pack> <version> <client> <sous-domaine>` | Lancer un provisioning (→ dry-run + confirmation) |
| `/jobs` | Lister les jobs récents et leur statut |
| `/job <id>` | Détail d'un job (trace, erreurs) |
| `/instances` | Lister les instances déployées |
| `/migrate <source> <client> <sous-domaine>` | Migrer un client legacy (Phase 4) |
| `/restore <instance> <backup>` | Restaurer depuis sauvegarde (Phase 5) |
| `/sync_catalog` | Re-synchroniser le catalogue depuis GitHub |
| `/version` | Version du bot |

Écritures et actions destructives réservées aux **admins** (`ADMIN_TELEGRAM_IDS`), avec confirmation ✅/❌.

---

## 12. Feuille de route

| Phase | Contenu | Effort estimé |
|---|---|---|
| **0 — Cadrage & socle** | Fork structure Alert Bot, schéma DuckDB (`clone_jobs`, `pack_versions`, `pending_actions`, `instances`), figer conventions d'artefact (tag GitHub + `documents_<pack>_<version>.zip`) | 0,5–1 j |
| **1 — Provisioning Coolify (cœur)** | VPS de test dédié, `CoolifyConnector`, `GitHubConnector`, `/provision` Telegram → dry-run → confirmation → déploiement, base MariaDB, envs `DOLI_*`, montage volume code | 3–4 j |
| **2 — Documents & injection client** | `DocumentsMover` (zip master → VPS), injection client API Dolibarr (raison sociale, XOF, admin), journalisation Sheets/Agenda | 2 j |
| **3 — Catalogue & module de publication** | Module Dolibarr de publication (PHP, GitHub + zip), `catalog_sync` (releases → `pack_versions`), `/packs` enrichi | 2–3 j |
| **4 — Migration legacy → conteneur** | Mode `migrate` : dump live + sentinelles, import dans conteneur neuf | 2–3 j |
| **5 — Cycle de vie & robustesse** | Rollback, suspension/restauration/résiliation, `restore`, jonction Alert Bot (`expires_on`) | 2 j |
| **6 — Déclenchement commercial** | Webhook WooCommerce `order.completed`, idempotence `woo_order_id`, API appelée par Laravel | selon JG Hosting |

Total réaliste phases 0–3 : **environ une semaine et demie** de travail effectif.

---

## 13. Points à valider au démarrage

1. **Point de montage exact de `custom/`** sur l'image Dolibarr officielle réellement déployée (§4.4) — à caler au premier déploiement test.
2. **Payloads Coolify 4.1.2 réels** vs spec OpenAPI (§5) — valider chaque appel contre l'instance, écarts spec/réalité connus.
3. **Format d'artefact figé** (tag GitHub + convention de nommage du zip documents) avant d'écrire la Phase 1 — c'est le point ouvert n°4 de `JG_Hosting_Synthese.md`.
4. ~~**Repo GitHub** : un repo par pack, ou repo commun ?~~ **Tranché (§4.5) : un repo privé par pack** (`jgh-pack-<pack>`), tags semver simples, une deploy key Coolify par repo.
5. **VPS de test** : dimensionnement et rattachement à Coolify (serveur de ressources dédié aux déploiements de test).

---

## 14. Principes non négociables (récapitulatif)

- **Coolify exécute, le bot orchestre** — aucune logique métier dans Coolify.
- **Une seule image par version de Dolibarr** — jamais une image par pack ou par client.
- **Code du pack en volume read-only, documents tenant en volume read-write** — jamais mélangés.
- **Jamais d'action destructive silencieuse** — file `pending` + confirmation Telegram.
- **Dry-run par défaut**, idempotence par clé de job, trace intégrale conservée.
- **Jeton Coolify scopé** (`write`/`deploy`), jamais `root` ; secrets hors repo.
- **Le module de publication vit dans Dolibarr**, le bot consomme le catalogue.
- **Un repo GitHub privé par pack** (`jgh-pack-<pack>`), une deploy key Coolify par repo — jamais un repo commun.
- **Clonage live = migration one-shot**, jamais voie de provisioning nominale.
- **Code, messages et docs en français** · fuseau Africa/Dakar (= UTC).
```
