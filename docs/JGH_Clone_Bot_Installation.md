# JGH Clone Bot — Guide d'installation

Ce guide décrit le déploiement du JGH Clone Bot sur le **VPS d'exploitation**
(`57.131.27.63`, `vps-1d022aa7`), **partagé avec JGH Alert Bot**. Il intègre les
règles de cohabitation des deux bots et les erreurs à éviter.

> **Contexte** : Clone Bot est l'outil d'orchestration du provisioning. Il tourne
> sur le VPS d'exploitation mais **ne déploie rien dessus** — les conteneurs
> Dolibarr partent sur le VPS Coolify via l'API. Ce VPS orchestre, il n'héberge
> pas de flotte client.
>
> **Version du guide** : à jour de l'installation gitifiée (dossier
> `/opt/jgh-clone-bot-app`) et de l'extra `[job-queue]` requis pour le suivi de
> déploiement.

---

## 0. Règle d'or de cohabitation avec Alert Bot

Les deux bots partagent la machine mais **rien d'autre**.

| Élément | Alert Bot (existant) | Clone Bot (ce guide) | Partage interdit |
|---|---|---|---|
| Token Telegram | `@jgh_alert_bot` | **nouveau** `@jgh_clone_bot` | ⚠️ **Critique** |
| Fichier DuckDB | `/opt/jgh-alert-bot/jgh.duckdb` | `/opt/jgh-clone-bot-app/clone.duckdb` | Oui |
| Utilisateur système | `jgh-bot` | `jgh-clone` | Oui (sécurité) |
| Dossier de code | `/opt/jgh-alert-bot` | `/opt/jgh-clone-bot-app` | Oui |
| Service systemd | `jgh-alert-bot` | `jgh-clone-bot` | Oui |
| venv Python | son venv | son venv | Oui (versions) |

> ⚠️ **L'incident le plus classique** : deux processus partageant le **même token
> Telegram** provoquent `Conflict: terminated by other getUpdates request`,
> doublons et messages perdus. Clone Bot doit avoir **son propre bot** créé via
> @BotFather. Ne jamais réutiliser le token d'Alert Bot.

---

## Note sur les chemins : dossier `-app` vs service

Point qui prête à confusion, à retenir :

- Le **dossier de code** est `/opt/jgh-clone-bot-app` (un clone Git propre).
- Le **service systemd** s'appelle `jgh-clone-bot` (**sans** `-app`).

Le dossier a été séparé du home de l'utilisateur `jgh-clone` (qui est
`/opt/jgh-clone-bot`, encombré de `.bashrc`, `venv`, etc.) pour obtenir un dépôt
Git propre où `git pull` fonctionne. L'ancien dossier `/opt/jgh-clone-bot` peut
subsister comme filet de sécurité.

---

## Prérequis

- Accès root/sudo sur `57.131.27.63`
- Python 3.12 (déjà présent, utilisé par Alert Bot)
- Un **nouveau** bot Telegram créé via @BotFather (token en main)
- Accès sortant HTTPS vers l'instance Coolify (`http://51.255.204.248:8000`)
- Le dépôt GitHub privé `jaxaayannuaire/jgh-clone-bot`

---

## 1. Utilisateur système dédié

Utilisateur distinct d'Alert Bot — cloisonnement des secrets (Clone Bot détient
un jeton Coolify ; il **écrit** sur la prod, alors qu'Alert Bot est en lecture
seule).

```bash
useradd -r -m -d /opt/jgh-clone-bot -s /bin/bash jgh-clone
```

> ⚠️ **Ne pas utiliser un shell `nologin`.** Le déploiement (`git pull`) et le
> debug (`su - jgh-clone`) nécessitent un vrai shell bash.

## 2. Cloner le dépôt (dans le dossier `-app`)

Dépôt **privé** (`jaxaayannuaire/jgh-clone-bot`), cloné dans un dossier dédié
**distinct du home** de l'utilisateur.

```bash
cd /opt
git clone https://github.com/jaxaayannuaire/jgh-clone-bot.git jgh-clone-bot-app
chown -R jgh-clone:jgh-clone /opt/jgh-clone-bot-app
```

## 3. Environnement Python (venv séparé, avec l'extra job-queue)

venv propre à Clone Bot — jamais partagé avec Alert Bot.

```bash
cd /opt/jgh-clone-bot-app
sudo -u jgh-clone python3 -m venv venv
sudo -u jgh-clone venv/bin/pip install -r requirements.txt
```

> ⚠️ **L'extra `[job-queue]` est indispensable.** Le `requirements.txt` déclare
> `python-telegram-bot[job-queue]` : il installe `apscheduler`, requis pour le
> **suivi de fin de déploiement** en tâche de fond. Sans lui, `job_queue` est
> `None` et les notifications de fin ne partent pas. Le bot le signale au
> démarrage (log « job_queue actif » ou « job_queue indisponible »).

## 4. Fichier `.env`

Copier `.env.example` et remplir.

```bash
cd /opt/jgh-clone-bot-app
sudo -u jgh-clone cp .env.example .env
sudo -u jgh-clone nano .env
```

Variables essentielles :

```
# --- Telegram (NOUVEAU bot, token distinct d'Alert Bot !) ---
TELEGRAM_BOT_TOKEN=...                              # @jgh_clone_bot
ALLOWED_TELEGRAM_IDS=...
ADMIN_TELEGRAM_IDS=...                              # écritures/déploiements

# --- Coolify (control plane de provisioning) ---
COOLIFY_BASE_URL=http://51.255.204.248:8000
COOLIFY_TOKEN=...                                   # jeton read+write+deploy
COOLIFY_SERVER_UUID=gs2b89lqrqq4z14i7td65b6u
COOLIFY_PROJECT_UUID=f620zkz5f38zrhk957723euq
COOLIFY_ENVIRONMENT_NAME=production
COOLIFY_ENVIRONMENT_UUID=o8l88sw997b1zu01lxkrqtj1
COOLIFY_TIMEOUT=30

# --- Base locale (fichier distinct d'Alert Bot) ---
DB_PATH=clone.duckdb

# --- Domaine (test sslip.io ; prod : s1.yessalerp.com) ---
DOMAIN_SUFFIX=51.255.204.248.sslip.io

# --- Catalogue de packs (deploy key Coolify par pack) ---
DEFAULT_PACK=pos
PACK_POS_REPOSITORY=git@github.com:jaxaayannuaire/jgh-pack-pos.git
PACK_POS_BRANCH=main
PACK_POS_DEPLOY_KEY_UUID=...                        # UUID de la deploy key dans Coolify
```

> ⚠️ **Vérifier deux fois** : `TELEGRAM_BOT_TOKEN` ≠ celui d'Alert Bot, et
> `DB_PATH` distinct (`clone.duckdb`, pas `jgh.duckdb`).
>
> **Note images privées** : les images de pack sont sur `ghcr.io` (privées).
> C'est **l'hôte de déploiement** (VPS Coolify) qui doit être authentifié via
> `docker login ghcr.io`, pas le bot. Le bot n'a pas besoin de token GitHub :
> il pilote seulement Coolify, qui clone les repos via leurs deploy keys.

## 5. Deploy keys des packs

Les clés de déploiement des repos `jgh-pack-*` sont enregistrées **dans
Coolify** (une par repo), pas dans le bot. Le `.env` ne référence que leur
**UUID Coolify** (`PACK_POS_DEPLOY_KEY_UUID`), jamais la clé elle-même.

Pour chaque nouveau pack : générer une paire de clés, déposer la publique en
deploy key du repo GitHub (lecture seule), la privée dans Coolify (Private Keys),
puis reporter l'UUID Coolify dans le `.env`.

## 6. Service systemd

`/etc/systemd/system/jgh-clone-bot.service` :

```ini
[Unit]
Description=JGH Clone Bot
After=network.target

[Service]
Type=simple
User=jgh-clone
WorkingDirectory=/opt/jgh-clone-bot-app
ExecStart=/opt/jgh-clone-bot-app/venv/bin/python bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> ⚠️ **`WorkingDirectory=/opt/jgh-clone-bot-app` indispensable** : `load_dotenv()`
> cherche le `.env` dans le répertoire courant. Bien pointer sur le dossier
> `-app` (le dépôt Git), pas sur l'ancien `/opt/jgh-clone-bot`.

```bash
systemctl daemon-reload
systemctl enable jgh-clone-bot
systemctl start jgh-clone-bot
journalctl -u jgh-clone-bot -n 30 --no-pager
```

Au démarrage, vérifier les deux lignes clés dans les logs :
- `job_queue actif : notification de fin de déploiement OK.`
- `JGH Clone Bot démarré (allowed=…, admins=…, packs=…)`

## 7. Vérifier la cohabitation

```bash
# Deux services distincts, tous deux actifs
systemctl status jgh-alert-bot jgh-clone-bot --no-pager | grep Active

# Une seule instance de chaque bot (piège du token partagé)
ps aux | grep -E 'bot\.py' | grep -v grep
# → exactement 2 lignes : un python d'Alert Bot (jgh-bot, /opt/jgh-alert-bot),
#   un de Clone Bot (jgh-clone, /opt/jgh-clone-bot-app).
```

> ⚠️ Si Telegram renvoie `Conflict: terminated by other getUpdates request`,
> le token est partagé avec un autre bot. Vérifier `TELEGRAM_BOT_TOKEN`.

## 8. Valider le bot

Depuis Telegram, avec un compte admin :
- `/version` → doit afficher la version, Coolify « joignable », le nombre de packs.
- `/packs` → doit lister le pack POS avec ✅ (deploy key configurée).

---

## Déploiement d'une mise à jour

Flux Git propre. **Le `git pull` se fait en tant que `jgh-clone`** (propriétaire
du dépôt), sinon Git refuse pour « dubious ownership ».

```bash
# 1. Récupérer le nouveau code (utilisateur propriétaire du dépôt)
sudo -u jgh-clone git -C /opt/jgh-clone-bot-app pull

# 2. Si requirements.txt a changé : réinstaller les dépendances
sudo -u jgh-clone /opt/jgh-clone-bot-app/venv/bin/pip install \
    -r /opt/jgh-clone-bot-app/requirements.txt

# 3. Redémarrer le service (root)
systemctl restart jgh-clone-bot
journalctl -u jgh-clone-bot -n 20 --no-pager
```

> **Migrations de base** : le schéma DuckDB se met à jour tout seul au démarrage
> (`ALTER TABLE ADD COLUMN IF NOT EXISTS`). Une mise à jour qui ajoute des colonnes
> ne demande aucune action manuelle ; la base existante est migrée sans perte.

---

## Erreurs à éviter (mémo)

1. **Token Telegram partagé** → `Conflict: terminated by other getUpdates`.
   Un bot = un token. Vérifier `ps aux | grep bot.py`.
2. **Même fichier DuckDB qu'Alert Bot** → corruption/verrous. `DB_PATH` distinct.
3. **`WorkingDirectory` systemd pointant sur le mauvais dossier** → `.env`
   introuvable. Bien viser `/opt/jgh-clone-bot-app`.
4. **`git pull` en tant que root** → « dubious ownership ». Utiliser
   `sudo -u jgh-clone git -C /opt/jgh-clone-bot-app pull`.
5. **Oublier l'extra `[job-queue]`** → `job_queue` est `None`, pas de
   notification de fin. Réinstaller `requirements.txt`.
6. **venv partagé avec Alert Bot** → conflits de versions. Un venv par bot.
7. **Déployer un conteneur sur CE VPS** → non. Les déploiements partent sur le
   VPS Coolify. Ce VPS orchestre uniquement.
8. **Windows/PowerShell local** : pas de chaînage `&&`, une commande par ligne.
   Déploiement par git.
