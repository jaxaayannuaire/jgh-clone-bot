# CLAUDE.md — JGH Clone Bot

Bot Telegram d'orchestration de provisioning pour YessalERP / JGH Hosting. Il pilote **Coolify par API REST** (Coolify gère le SSH vers ses cibles en interne) pour déployer des instances Dolibarr sectorisées ("packs") pour des clients SME. Code, messages et docs en **français**.

## Règle absolue — Git

**Je ne fais JAMAIS de `git commit` ni de `git push` de moi-même.**
Jaxaay pousse sur GitHub manuellement (`jaxaayannuaire/jgh-clone-bot`). Je prépare les fichiers et une entrée de changelog, puis je m'arrête. Même règle sur le VPS : le flow `sudo -u jgh-clone git pull` sur `/opt/jgh-clone-bot-app` reste déclenché par Jaxaay, jamais par moi.

## Principe architectural non négociable

**Séparation control plane / data plane stricte :**
- Ce bot orchestre la logique métier de provisioning (Telegram + jobs)
- **Coolify** exécute le cycle de vie des conteneurs Docker via son API REST — je ne fais jamais de SSH direct vers les cibles de déploiement
- **Dolibarr** possède la comptabilité
- **WooCommerce** possède les abonnements et l'identité client (source de vérité, lecture seule pour ce bot)
- **DuckDB** = source de vérité locale pour l'état des jobs et le catalogue de packs

Aucune écriture circulaire entre ces couches.

## Stack & architecture

```
jgh-clone-bot/
├── bot.py                    # point d'entrée (JobQueue + handlers)
├── db/                       # schema.sql, duckdb_client.py (connexion unique)
├── connectors/
│   ├── coolify_connector.py  # API REST /api/v1 (create/deploy/env/delete)
│   ├── github_connector.py   # releases, tags, checkout code+dump
│   └── documents_mover.py    # zip master → VPS déploiement (SSH/SFTP)
├── integrations/              # dolibarr_client, sheets_sync, calendar_sync — RÉUTILISÉS d'Alert Bot
├── modes/                     # provision.py, migrate.py, restore.py
├── jobs/                      # job_runner.py, catalog_sync.py
├── core/                      # confirmation.py (file pending), dry_run.py
├── wizard_engine.py           # moteur déclaratif (choice/text/confirm) — copié, pas lib partagée
├── wizard_store.py            # état wizard persisté DuckDB
└── tests/
```

Infrastructure : VPS gitifié `/opt/jgh-clone-bot-app`, utilisateur `jgh-clone`. Repo GitHub privé `jaxaayannuaire/jgh-clone-bot`.

## Le modèle de packs (figé — ne pas réinventer)

- 5 packs au catalogue : Tambali, Asso, Pro, Immo, POS. Démarrage effectif : POS + Pro.
- **Une image Docker par version de Dolibarr encore en prod (2-3 vivantes), jamais par variante de pack (20).** Ce qui distingue un pack est monté par-dessus : `DOLI_ENABLE_MODULES` (env), code `custom/` (volume, tag GitHub), seed (dump SQL importé), documents d'exemple (zip monté).
- 2 versions par pack/an, seules les 2 dernières actives ; les plus anciennes archivées puis purgées.
- **Stratégie B validée** (deux images baked par pack sur ghcr.io) — Stratégie A (image partagée + bind-mount) abandonnée pour bug Coolify. Voir `DECISIONS.md` avant de reconsidérer.
- Documents de référence = **toujours des données fictives**, jamais un vrai client.

## Règles non négociables

- **Toute action destructive/coûteuse passe par confirmation** — file `pending_actions` + boutons Telegram inline, jamais d'exécution directe
- Écritures et actions destructives réservées aux **admins** (`ADMIN_TELEGRAM_IDS`)
- WooCommerce reste **lecture seule** depuis ce bot — jamais d'écriture vers les commandes/abonnements
- Mapping produit→pack toujours via `.env` (`PACK_<X>_PRODUCT_ID`), jamais hardcodé
- Job reservation **atomique** — éviter toute double-exécution par race condition sur double-clic

## Pièges connus (ne pas réintroduire)

- **User-Agent WooCommerce** — le UA par défaut de `python-requests` est bloqué par les plugins firewall WordPress. Toujours un UA nommé custom.
- **Grace period conteneur** — `GRACE_MAX_ATTEMPTS=8` × 15s avant de déclarer un échec de démarrage.
- Le bouton Deploy doit **survivre à un restart du bot** — relire l'order fresh depuis WooCommerce via `order_id`, ne jamais garder l'état seulement en mémoire.

## Docs de référence (à lire à la demande)

- `JGH_Clone_Bot_Architecture.md` v1.2 — architecture détaillée
- `JGH_Clone_Bot_Installation.md`
- `JGH_Clone_Passation.md` — passation depuis Alert Bot
- `DECISIONS.md` — journal des approches abandonnées et leur rationale
- `comparaison-cloudpanel-vs-coolify-yessalerp.md`

## Workflow attendu

1. Je lis le contexte pertinent avant de modifier
2. Je code, je mets à jour `CHANGELOG.md` et `DECISIONS.md` si une approche est écartée
3. Je résume les fichiers modifiés et propose une entrée de changelog
4. Jaxaay relit, puis commit/push et déploiement manuel lui-même
