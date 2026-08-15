# JGH Clone Bot — Guide d'installation

Ce guide décrit le déploiement du JGH Clone Bot sur le **VPS d'exploitation**
(`57.131.27.63`, `vps-1d022aa7`), **partagé avec JGH Alert Bot**. Il intègre les
règles de cohabitation des deux bots et reprend les erreurs à éviter apprises sur
Alert Bot.

> **Contexte** : Clone Bot est l'outil d'orchestration du provisioning. Il tourne
> sur le VPS d'exploitation mais **ne déploie rien dessus** — les conteneurs
> Dolibarr partent sur le VPS de test/données via l'API Coolify. Ce VPS orchestre,
> il n'héberge pas de flotte client.

---

## 0. Règle d'or de cohabitation avec Alert Bot

Les deux bots partagent la machine mais **rien d'autre**. Avant tout, retenir :

| Élément | Alert Bot (existant) | Clone Bot (ce guide) | Partage interdit |
|---|---|---|---|
| Token Telegram | `@jgh_alert_bot` | **nouveau** `@jgh_clone_bot` | ⚠️ **Critique** |
| Fichier DuckDB | `/opt/jgh-alert-bot/jgh.duckdb` | `/opt/jgh-clone-bot/clone.duckdb` | Oui |
| Utilisateur système | `jgh-bot` | `jgh-clone` | Oui (sécurité) |
| Dossier d'install | `/opt/jgh-alert-bot` | `/opt/jgh-clone-bot` | Oui |
| Service systemd | `jgh-alert-bot` | `jgh-clone-bot` | Oui |
| venv Python | son venv | son venv | Oui (versions) |

> ⚠️ **L'incident le plus classique** : deux processus qui partagent le **même
> token Telegram** provoquent `Conflict: terminated by other getUpdates request`,
> doublons et messages perdus. Clone Bot doit avoir **son propre bot** créé via
> @BotFather. C'est exactement le bug `dndtalk.service` documenté sur Alert Bot,
> transposé. Ne jamais réutiliser le token d'Alert Bot.

---

## Prérequis

- Accès root/sudo sur `57.131.27.63`
- Python 3.12 (déjà présent, utilisé par Alert Bot)
- Un **nouveau** bot Telegram créé via @BotFather (token en main)
- Accès sortant HTTPS vers l'instance Coolify (`http://51.255.204.248:8000`)
- Accès sortant vers `api.github.com` (lecture des repos privés `jgh-pack-*`)
- Le dépôt GitHub privé `jaxaayannuaire/jgh-clone-bot`

---

## 1. Utilisateur système dédié

Utilisateur distinct d'Alert Bot — cloisonnement des secrets (Clone Bot détient
des jetons Coolify et des deploy keys GitHub ; il **écrit** sur la prod, alors
qu'Alert Bot est en lecture seule).

```bash
useradd -r -m -d /opt/jgh-clone-bot -s /bin/bash jgh-clone
```

> ⚠️ **Erreur à éviter (héritée d'Alert Bot)** : ne pas utiliser un shell
> `nologin`. Le déploiement (`git pull`, `pip`) et le debug (`su - jgh-clone`)
> nécessitent un vrai shell bash.

## 2. Cloner le dépôt

Dépôt **privé** (`jaxaayannuaire/jgh-clone-bot`).

```bash
cd /opt/jgh-clone-bot
sudo -u jgh-clone git clone https://github.com/jaxaayannuaire/jgh-clone-bot.git .
```

## 3. Environnement Python (venv séparé)

venv propre à Clone Bot — jamais partagé avec Alert Bot (dépendances
divergentes : Clone Bot a besoin de `requests` pour Coolify).

```bash
cd /opt/jgh-clone-bot
sudo -u jgh-clone python3 -m venv venv
sudo -u jgh-clone venv/bin/pip install -r requirements.txt
```

## 4. Fichier `.env`

Copier `.env.example` et remplir. **Chemins de credentials en absolu**
(leçon Alert Bot : un chemin relatif échoue selon le répertoire de travail).

```bash
cp .env.example .env
nano .env
```

Variables essentielles :

```
# --- Coolify (control plane de provisioning) ---
COOLIFY_BASE_URL=http://51.255.204.248:8000
COOLIFY_TOKEN=...                                  # jeton read+write+deploy
COOLIFY_SERVER_UUID=gs2b89lqrqq4z14i7td65b6u
COOLIFY_PROJECT_UUID=f620zkz5f38zrhk957723euq
COOLIFY_ENVIRONMENT_NAME=production
COOLIFY_ENVIRONMENT_UUID=o8l88sw997b1zu01lxkrqtj1
COOLIFY_TIMEOUT=30

# --- GitHub (catalogue + repos de packs) ---
GITHUB_TOKEN=...                                   # permission repo (privés)

# --- Telegram (NOUVEAU bot, token distinct d'Alert Bot !) ---
TELEGRAM_BOT_TOKEN=...                              # @jgh_clone_bot
ALLOWED_TELEGRAM_IDS=...
ADMIN_TELEGRAM_IDS=...                              # écritures/déploiements

# --- Base locale (fichier distinct d'Alert Bot) ---
DB_PATH=/opt/jgh-clone-bot/clone.duckdb
TIMEZONE=Africa/Dakar
```

> ⚠️ **Vérifier deux fois** : `TELEGRAM_BOT_TOKEN` ≠ celui d'Alert Bot, et
> `DB_PATH` pointe bien vers `clone.duckdb` (pas `jgh.duckdb`).

## 5. Clés et secrets (dossier `keys/`)

```bash
mkdir -p /opt/jgh-clone-bot/keys
# (Selon les besoins : clé pour le transport des documents master → VPS déploiement,
#  déposée ici quand le mode migrate/documents sera implémenté.)
chown jgh-clone:jgh-clone /opt/jgh-clone-bot/keys/*
chmod 600 /opt/jgh-clone-bot/keys/*
```

Le dossier `keys/`, `.env`, `*.duckdb`, `*.json` sont dans `.gitignore` —
**jamais commités**.

> **Note deploy keys GitHub** : les clés de déploiement des repos `jgh-pack-*`
> sont enregistrées **dans Coolify** (une par repo), pas dans le bot. Le bot
> référence leur UUID Coolify, il ne détient pas les clés elles-mêmes.

## 6. Service systemd

`/etc/systemd/system/jgh-clone-bot.service` :

```ini
[Unit]
Description=JGH Clone Bot
After=network.target

[Service]
Type=simple
User=jgh-clone
WorkingDirectory=/opt/jgh-clone-bot
ExecStart=/opt/jgh-clone-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> ⚠️ **`WorkingDirectory=/opt/jgh-clone-bot` indispensable** : `load_dotenv()`
> cherche le `.env` dans le répertoire courant (leçon Alert Bot).

```bash
systemctl daemon-reload
systemctl enable jgh-clone-bot
systemctl start jgh-clone-bot
journalctl -u jgh-clone-bot -n 30 --no-pager
```

## 7. Vérifier la cohabitation

Après démarrage, contrôler qu'on n'a pas de conflit avec Alert Bot :

```bash
# Deux services distincts, tous deux actifs
systemctl status jgh-alert-bot jgh-clone-bot --no-pager | grep Active

# Une seule instance de chaque bot (piège dndtalk.service)
ps aux | grep -E 'bot\.py' | grep -v grep
# → doit montrer exactement 2 lignes : un python d'Alert Bot, un de Clone Bot,
#   sous des utilisateurs différents (jgh-bot vs jgh-clone).
```

> ⚠️ Si Telegram renvoie `Conflict: terminated by other getUpdates request` dans
> `journalctl -u jgh-clone-bot`, c'est que le token est partagé avec un autre
> bot. Vérifier que `TELEGRAM_BOT_TOKEN` est bien celui du nouveau bot.

---

## 8. Valider la connexion Coolify (avant d'utiliser le bot)

Avant de se fier au bot, valider le connecteur avec le script de test isolé
(lectures seules, sans effet de bord) :

```bash
su - jgh-clone -s /bin/bash
cd /opt/jgh-clone-bot
venv/bin/python test_coolify.py          # lectures seules
# puis, quand un repo de pack + deploy key existent :
venv/bin/python test_coolify.py --deploy # création/suppression d'app test
exit
```

L'étape lectures doit confirmer : API joignable, serveur trouvé, reachable et
usable. Le `wildcard_domain` non configuré est **normal en test**.

---

## Déploiement d'une mise à jour

Même pattern qu'Alert Bot :

```bash
su - jgh-clone -s /bin/bash
cd /opt/jgh-clone-bot && git pull
# si requirements changé : venv/bin/pip install -r requirements.txt
exit
systemctl restart jgh-clone-bot
journalctl -u jgh-clone-bot -n 30 --no-pager
```

---

## Erreurs à éviter (mémo, dont héritées d'Alert Bot)

1. **Token Telegram partagé** → `Conflict: terminated by other getUpdates`.
   Un bot = un token. Vérifier `ps aux | grep bot.py`.
2. **Même fichier DuckDB que Alert Bot** → corruption/verrous. `DB_PATH` distinct.
3. **`WorkingDirectory` systemd oublié** → `.env` introuvable, variables vides.
4. **Chemin relatif pour un credential** → échoue selon le CWD. Toujours absolu.
5. **Lire l'env au niveau module au lieu de `__init__`** → variables vides à
   l'import (bug classique Alert Bot). Lire l'env dans `__init__`, après `load_dotenv()`.
6. **venv partagé** → conflits de versions. Un venv par bot.
7. **Déployer un conteneur sur CE VPS** → non. Les déploiements partent sur le
   VPS de test/données via Coolify. Ce VPS orchestre uniquement.
8. **Windows/PowerShell local** : pas de chaînage `&&`, une commande par ligne.
   Déploiement par git.
```
