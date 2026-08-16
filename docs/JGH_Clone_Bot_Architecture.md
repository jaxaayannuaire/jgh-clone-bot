# JGH Clone Bot — Architecture

> **Version 1.2** — août 2026. Ce document décrit **uniquement l'architecture
> validée et testée**. Les décisions explorées puis abandonnées (ex. montage du
> code en volume) sont consignées dans `DECISIONS.md`, pas ici : l'architecture
> reste une référence fiable de l'état réel du système.
>
> Changements majeurs depuis la v1.1 : passage à la **Stratégie B** (deux images
> cuites par pack, publiées sur GitHub Container Registry) ; documents injectés
> au premier boot par un wrapper d'entrypoint ; suivi de fin de déploiement ;
> gestion du type d'instance (test/client) et suppression sécurisée.

---

## 1. Positionnement et périmètre

### 1.1 Ce qu'est JGH Clone Bot

JGH Clone Bot est le **composant d'exécution du provisioning** de la plateforme
JGH Hosting / YessalERP. Il déploie des instances **Dolibarr sectorisées**
(packs) via l'API Coolify, piloté par Telegram. À terme, une version de cet
outil sera intégrée à YessalERP pour déployer des packs Dolibarr, WordPress et
autres.

Il fait partie d'un ensemble de trois briques qui partagent la même
infrastructure et les mêmes patterns :

| Brique | Rôle | Posture |
|---|---|---|
| **JGH Alert Bot** (opérationnel) | Supervision, échéances, alertes | **Lecture seule** |
| **JGH Clone Bot** (ce document) | Provisioning, suppression, migration | **Écrit et exécute** |
| **JG Hosting / YessalERP** | Couche commerciale + control plane Laravel | Orchestration |

### 1.2 Décision structurante : Coolify est le cœur

**Coolify (conteneurs Docker) est le paradigme nominal du provisioning ; le
clonage live ISPConfig→CloudPanel n'est plus qu'un outil de migration
one-shot.**

L'image Docker Dolibarr se configure par variables d'environnement
(`DOLI_DB_*`, `DOLI_URL_ROOT`, `DOLI_INSTALL_AUTO`…), ce qui **supprime l'étape
`sed -i` sur `conf.php`** — la partie la plus fragile du pipeline historique.

### 1.3 Les trois modes du bot

| Mode | Usage | Paradigme | Priorité |
|---|---|---|---|
| **provision** | Nouveau client → nouvelle instance | Coolify + deux images de pack (ghcr.io) | Nominal |
| **migrate** | Client legacy ISPConfig/CloudPanel → conteneur | Dump live → conteneur neuf | One-shot (plus tard) |
| **restore** | Restauration depuis sauvegarde | Réhydratation base + volume documents | Plus tard |

> Le mode `provision` est le cœur, validé de bout en bout. Les modes `migrate`
> et `restore` viendront ensuite.

### 1.4 Ce que JGH Clone Bot n'est PAS

- **Pas le module de publication de packs.** La transformation d'une instance de
  référence en artefacts de pack est faite par `publish_pack.py` +
  `build_pack_image.sh` (composants distincts, §7). Le bot **consomme** les
  images publiées, il ne les produit pas.
- **Pas le control plane commercial.** WooCommerce/Laravel restent maîtres de
  l'état commercial. Le bot exécute des ordres de provisioning.
- **Pas un orchestrateur généraliste dès le départ.** On commence par Dolibarr
  (cas le plus mûr), on généralise après.

---

## 2. Ce qui est hérité de JGH Alert Bot

JGH Clone Bot réutilise le socle éprouvé plutôt que de réinventer.

### Patterns réutilisés tels quels
- **Contrôle-plan / données-plan** : le bot orchestre, Coolify exécute.
- **Connexion DuckDB unique stable** + schéma versionné (migration douce).
- **File `pending_actions` + boutons inline Telegram ✅/❌** : toute action
  destructive/coûteuse passe par le même circuit de confirmation.
- **Secrets hors repo**, service systemd, déploiement par git, langue française,
  fuseau Africa/Dakar.

### Le changement de posture majeur
**Alert Bot est lecture seule ; Clone Bot écrit et exécute sur des serveurs de
production.** C'est le sujet de sécurité central (§8). Clone Bot agit par
**jetons Coolify scopés** (`write`/`deploy`) — surface réduite et versionnée,
pas un SSH root large.

---

## 3. Architecture générale

```
┌────────────────────────────────────────────────────────────────┐
│                    VPS D'EXPLOITATION (bot)                      │
│                    57.131.27.63 — CloudPanel                    │
│                                                                 │
│  ┌──────────┐   /provision      ┌────────────────────────────┐ │
│  │ Telegram │◄──/delete────────►│    JGH Clone Bot (py)       │ │
│  │  (admin) │   /instances      │    python-telegram-bot      │ │
│  └──────────┘   ✅/❌ confirm    │    + JobQueue (suivi)       │ │
│                                 │  ┌──────────────────────┐   │ │
│                                 │  │  DuckDB clone.duckdb │   │ │
│                                 │  │  clone_jobs          │   │ │
│                                 │  │  pending_actions     │   │ │
│                                 │  └──────────────────────┘   │ │
│                                 │  ┌──────────────────────┐   │ │
│                                 │  │  CoolifyConnector    │   │ │
│                                 │  └──────────┬───────────┘   │ │
│                                 └─────────────┼───────────────┘ │
└───────────────────────────────────────────────┼─────────────────┘
                       API REST /api/v1  ▲       │
                                         │       │
                    ┌────────────────────┴──┐    │  git clone (deploy key)
                    │  VPS COOLIFY           │    │
                    │  (control plane)       │    ▼
                    │                        │  ┌──────────────────────┐
                    │  clone le repo du pack │  │ GitHub               │
                    │  tire les images ──────┼─►│  jgh-pack-<pack>      │
                    │  déploie 2 conteneurs  │  │  (docker-compose.yml) │
                    └───────────┬────────────┘  └──────────────────────┘
                                │  docker pull
                                ▼
                    ┌────────────────────────┐  ┌──────────────────────┐
                    │  ghcr.io (privé)       │  │ Instance déployée    │
                    │  jgh-pack-<pack>       │─►│  • dolib (Dolibarr)  │
                    │  jgh-pack-<pack>-db    │  │  • db (MariaDB+dump) │
                    │  jgh-dolibarr (commune)│  │  documents injectés  │
                    └────────────────────────┘  └──────────────────────┘
```

### Principes directeurs

- **Le bot ne parle jamais SSH aux conteneurs.** Il pilote Coolify par API
  REST ; Coolify gère l'exécution vers ses cibles en interne.
- **GitHub Container Registry (`ghcr.io`) est le registre d'images.** Tout le
  projet JGH y publie ses images (privées). Le self-hosted est réservé aux
  projets clients sur mesure.
- **Le repo Git d'un pack ne contient que son `docker-compose.yml`.** Le code,
  les données et les documents sont dans les images ghcr.io, pas dans le repo.
- **DuckDB est la source de vérité locale** de l'état des jobs et instances.
- **Toute action destructive/coûteuse passe par confirmation** (file `pending`,
  boutons Telegram ; saisie du nom pour les instances client).

---

## 4. Le modèle de packs — Stratégie B (validée)

### 4.1 Deux images cuites par pack

Chaque pack se matérialise en **deux images Docker autonomes**, publiées sur
`ghcr.io` (privées) :

| Image | Contenu |
|---|---|
| `ghcr.io/jaxaayannuaire/jgh-pack-<pack>:<version>` | Dolibarr + surcharges cœur + modules `custom/` + documents d'exemple + wrapper d'entrypoint |
| `ghcr.io/jaxaayannuaire/jgh-pack-<pack>-db:<version>` | MariaDB + dump SQL du pack (données d'exemple, modules activés) |

Une **image commune** `ghcr.io/jaxaayannuaire/jgh-dolibarr:<version-doli>`
(Dolibarr officiel + surcharges cœur) sert de base à l'image applicative de
chaque pack.

> **Ce qui distingue un pack d'un autre** (modules activés, TakePOS, etc.) vit
> dans le **dump SQL** de l'image `-db`, pas dans du code séparé. Le pack POS est
> le modèle de référence validé.

### 4.2 Les trois mécanismes d'injection (validés)

**Import du dump — par MariaDB, avant Dolibarr.**
Le dump du pack est placé dans `/docker-entrypoint-initdb.d/` de l'image `-db`.
MariaDB l'importe **à la création de la base**, avant que Dolibarr démarre.
Dolibarr trouve alors une base déjà peuplée et **saute son installation**
(message « Schema update is not required … Enjoy ! »). C'est ce qui résout le
conflit avec `DOLI_INSTALL_AUTO`.

**Documents — injectés au premier boot par un wrapper d'entrypoint.**
Les documents d'exemple (logos, images de catégories, PDF) sont cuits dans
l'image applicative sous `/opt/jgh/`. Un wrapper `jgh-entrypoint.sh` les
décompresse dans le volume `/var/www/documents` au premier démarrage (marqueur
`.jgh_documents_initialized` pour ne jamais écraser les données d'un client),
puis passe la main à l'entrypoint officiel (`docker-run.sh apache2-foreground`).
Ce wrapper est nécessaire car Dolibarr **saute** ses scripts `docker-init.d`
quand la base est déjà installée.

**Encadrement du dump SQL — clés étrangères.**
Le dump est encadré par `SET FOREIGN_KEY_CHECKS=0` (+ réactivation en pied) pour
un import fiable, car l'ordre alphabétique des tables provoque sinon des erreurs
de clés étrangères (1005 errno 150).

### 4.3 Cycle de vie des packs

Cinq packs au catalogue cible : **POS, Tambali, Asso, Pro, Immo**. Démarrage
effectif validé avec **POS** (`pos.yessal.com` comme référence).

Chaque version stable est taguée en semver (`1.0.0`). Une variante `-dev` (images
taguées `<version>-dev`) servira aux déploiements de test jetables.

### 4.4 Organisation GitHub

**Un dépôt Git privé par pack**, contenant uniquement le `docker-compose.yml` de
production et un README. Convention :

```
jaxaayannuaire/jgh-pack-pos
jaxaayannuaire/jgh-pack-pro
jaxaayannuaire/jgh-pack-tambali
jaxaayannuaire/jgh-pack-asso
jaxaayannuaire/jgh-pack-immo
```

**Une deploy key Coolify par repo** (privilège minimal : n'ouvre l'accès qu'à un
seul pack). Le bot lit une config statique `pack → {repo, deploy key, service}`
dans son catalogue `PACKS` (les UUID de deploy key viennent du `.env`).

Comme le repo ne contient qu'un fichier compose (quelques Ko), l'argument du
poids qui justifiait autrefois un repo par pack ne s'applique plus, mais la
séparation reste retenue pour l'**isolation d'accès** (une deploy key par pack)
et la lisibilité.

### 4.5 Le service applicatif s'appelle `dolib`

Convention imposée par le connecteur : tous les `docker-compose.yml` de packs
déclarent un service applicatif nommé **`dolib`**, que Coolify cible pour le
domaine (`docker_compose_domains`).

---

## 5. L'API Coolify — routes utilisées

Coolify **4.1.2**. API versionnée sous `/api/v1`, authentification par jeton
Laravel Sanctum, permissions granulaires (`read`/`write`/`deploy`). Le bot
utilise un jeton scopé `write`+`deploy` — **jamais `root`**.

| Besoin | Route | Permission |
|---|---|---|
| Créer l'app (repo privé, deploy key) | `POST /api/v1/applications/private-deploy-key` | write |
| Déclencher le déploiement | `POST /api/v1/applications/{uuid}/start` | deploy |
| Arrêter / redémarrer | `POST /api/v1/applications/{uuid}/stop` \| `/restart` | deploy |
| Détail d'une app (statut) | `GET /api/v1/applications/{uuid}` | read |
| Lister les déploiements actifs | `GET /api/v1/deployments` | read |
| Supprimer (résiliation, + volumes) | `DELETE /api/v1/applications/{uuid}?delete_volumes=true` | write |

Paramètres utiles à la création :
- **`instant_deploy`** (défaut `false`) : on sépare création et déploiement.
- **`force_domain_override`** (défaut `false`) : renvoie **HTTP 409** si le
  sous-domaine est déjà pris → jamais d'écrasement silencieux.
- **`docker_compose_domains`** : tableau d'objets `{name, domain}` (le `name` est
  le service du compose qui porte le domaine, ici `dolib` ; `domain` avec schéma
  `https://`).

**Authentification ghcr.io** : les images privées sont tirées grâce au
`docker login ghcr.io` de l'hôte de déploiement — aucune configuration de
registre supplémentaire dans Coolify n'a été nécessaire (validé).

> **Deux pièges connus :**
> 1. `removeSensitiveData` **ampute** certains champs de réponse si le jeton n'a
>    pas la lecture sensible — parsing défensif obligatoire.
> 2. Écarts spec/réalité de la doc Coolify : **valider chaque payload contre
>    l'instance 4.1.2 réelle** au moment de coder.

---

## 6. Flux de bout en bout — mode provision

```
/provision <client> <pack> [test]           (Telegram, admin)
        ↓
[1] Le bot résout le pack (catalogue) → deploy key, repo, service
    → insère un clone_job status='pending', dry_run=TRUE, instance_type
        ↓
[2] Message Telegram : plan (type test/client, pack, domaine) + ✅/❌
        ↓ (✅)
[3] Coolify : POST applications/private-deploy-key
    (repo du pack, deploy key, docker_compose_domains → service dolib)
        ↓
[4] POST {uuid}/start → déploiement
        ↓
[5] Coolify clone le repo, tire les 2 images ghcr.io
        ↓
[6] MariaDB importe le dump ; le wrapper injecte les documents ;
    Dolibarr trouve la base peuplée → « Enjoy ! »
        ↓
[7] Traefik obtient le certificat SSL (sous-domaine)
        ↓
[8] SUIVI (JobQueue) : le bot poll /deployments toutes les 15 s
        ↓
[9] Le déploiement disparaît de /deployments → terminé
    status app running → job 'active' (online_at) ✅
    sinon → job 'failed' 🔴 | timeout 12 min → ⚠️
        ↓
    Notification Telegram de fin (URL, version, durée, /job)
```

### Notification de fin (inspirée de l'e-mail d'installation OVH)

Le bot ne considère plus un job `active` dès le déclenchement : il **surveille**
le déploiement en tâche de fond (JobQueue, intervalle 15 s, timeout 12 min) et
envoie un message de fin — ✅ succès (URL, version Dolibarr, durée, lien `/job`),
🔴 échec (raison, diagnostic), ou ⚠️ dépassement de délai.

Détection : un déploiement figure dans `GET /deployments` avec
`status=in_progress`, puis **disparaît** de la liste une fois terminé. Le
résultat (succès/échec) se lit ensuite sur le `status` de l'application
(`running` = succès).

---

## 7. La publication de packs (composant distinct)

La transformation d'une instance Dolibarr de référence en artefacts de pack, puis
en images, est faite par deux outils **hors du flux du bot** :

- **`publish_pack.py`** : à partir de l'instance de référence, produit trois
  artefacts — le dump SQL (encadré FK, tables volatiles purgées, références de
  chemins neutralisées), l'archive `custom/` (modules maison), l'archive des
  documents d'exemple.
- **`build_pack_image.sh`** : assemble les artefacts en deux images
  (`jgh-pack-<pack>` et `jgh-pack-<pack>-db`) à partir de l'image commune, puis
  elles sont poussées sur ghcr.io.

Le bot **consomme** les images publiées ; il ne les produit pas. Cette frontière
est nette : publication en amont (rare, manuelle), déploiement en aval
(fréquent, piloté).

---

## 8. Sécurité des exécutions

Clone Bot écrit et exécute sur la production : c'est le sujet nouveau par rapport
à Alert Bot.

| Principe | Mise en œuvre |
|---|---|
| **Confirmation systématique** | Toute action destructive/coûteuse passe par la file `pending` + boutons ✅/❌ (admins only) |
| **Dry-run par défaut** | Le job affiche le plan complet (type, pack, domaine) **avant** toute écriture |
| **Suppression graduée** | Instance **test** : double confirmation par boutons. Instance **client** : saisie du nom exact (façon Coolify) |
| **Idempotence** | Clé de job unique (`idempotency_key`) : rejouer un provision ne double rien |
| **Privilège minimal** | Jeton Coolify scopé `write`+`deploy`, jamais `root` ; une deploy key par pack |
| **Trace intégrale** | stdout de chaque job conservé (`clone_jobs.stdout_log`), horodatage des étapes |
| **Refus des états incohérents** | Pas de suppression d'un déploiement encore en cours |
| **Secrets** | `.env` hors repo (gitignore), jetons Coolify/GitHub et deploy keys jamais versionnés ; token ghcr.io en lecture seule côté déploiement |
| **Conflits de domaine** | `force_domain_override=false` → 409 traité explicitement, jamais d'écrasement |

---

## 9. Modèle de données DuckDB

Connexion unique stable, schéma versionné avec **migration douce**
(`ALTER TABLE ADD COLUMN IF NOT EXISTS`) : les bases existantes sont mises à jour
au démarrage sans perte.

### Table `clone_jobs`

| Champ | Rôle |
|---|---|
| `id` | Identifiant du job/instance |
| `job_type` | `provision` \| `migrate` \| `restore` |
| `idempotency_key` | Rejeu sans doublon |
| `client_name` | Nom logique de l'instance |
| `subdomain` | Domaine complet déployé |
| `git_repository`, `git_branch` | Repo du pack déployé |
| `coolify_app_uuid` | UUID de l'app Coolify créée |
| `instance_type` | `client` (défaut, prudent) \| `test` — gouverne la suppression |
| `status` | `pending`\|`confirmed`\|`running`\|`active`\|`failed`\|`deleted` |
| `stdout_log` | Trace horodatée des étapes |
| `error_message` | Message d'erreur éventuel |
| `created_at`, `confirmed_at` | Horodatages de création / confirmation |
| `online_at` | Mise en ligne (premier passage `active`) |
| `resolved_at`, `deleted_at` | Résolution / suppression effective |

### Table `pending_actions`

File de confirmation (pattern Alert Bot) : `job_id`, `action_type`
(`provision`\|`delete`), `summary`, `status`
(`pending`\|`confirmed`\|`rejected`), horodatages.

---

## 10. Arborescence du projet

```
jgh-clone-bot/                     ← dépôt du bot (orchestrateur)
├── bot.py                         # handlers Telegram, catalogue de packs, suivi
├── coolify_connector.py           # client API Coolify v1
├── db/
│   ├── duckdb_client.py           # accès DuckDB
│   └── schema.sql                 # schéma (migration douce)
├── scripts/
│   └── publish_pack.py            # publication d'un pack (artefacts)
├── docs/                          # architecture, décisions, installation
├── .env.example
├── requirements.txt
└── CHANGELOG.md

jgh-pack-<pack>/                   ← un dépôt par pack (compose seul)
├── docker-compose.yml             # images ghcr.io, service dolib
└── README.md
```

---

## 11. Commandes Telegram

| Commande | Rôle |
|---|---|
| `/packs` | Liste le catalogue de packs déployables |
| `/provision <nom> <pack> [test]` | Déploie un pack (dry-run + confirmation) ; `test` marque une instance jetable |
| `/instances` | Liste les instances (type 🧪/👤, statut, mise en ligne) |
| `/delete <id>` | Résiliation (test : double bouton ; client : saisie du nom) |
| `/cancel` | Annule une suppression client en attente |
| `/jobs` | Déploiements récents |
| `/job <id>` | Détail d'un déploiement (statut, UUID, log horodaté) |
| `/version` | État du bot et de la connexion Coolify |

Toute action de déploiement/suppression est réservée aux admins
(`ADMIN_TELEGRAM_IDS`).

---

## 12. Feuille de route

**Validé et en production**
- Provisioning d'un pack depuis le catalogue (POS), déploiement Coolify depuis
  ghcr.io, notification de fin, gestion test/client, suppression sécurisée.
- Repo assaini, VPS gitifié, flux `git pull` propre.

**Prochains chantiers**
- Suivi étape par étape (jalons horodatés pendant le déploiement).
- Infos serveur : estimation instances disponibles/total selon RAM/CPU.
- Version dev/test des packs (`-dev`, jetables 14 j/30 j).
- Autres packs : Tambali, Asso, Pro, Immo.
- Phase domaines : `*.s1.yessalerp.com` + SSL (préfixe serveur par VPS).
- Déclencheur commercial : webhook WooCommerce/Laravel sur `order.completed`
  (idempotence par `woo_order_id`), quand la couche commerciale sera branchée.
- Collaboration Alert Bot ↔ Clone Bot.

---

## 13. Principes non négociables (récapitulatif)

- **Coolify exécute, le bot orchestre.** Jamais de SSH root large ; API scopée.
- **Deux images cuites par pack, publiées sur ghcr.io.** Ce qui distingue un pack
  vit dans son dump SQL et ses images, pas ailleurs.
- **Aucun bind mount de fichier** (contournement du bug Coolify — voir
  `DECISIONS.md`).
- **Confirmation avant toute action destructive** ; saisie du nom pour les
  instances client.
- **Secrets hors repo**, français, fuseau Africa/Dakar, déploiement par git.
- **Valider contre l'instance réelle** avant de figer un comportement d'API.
