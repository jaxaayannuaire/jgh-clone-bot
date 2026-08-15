# JGH Clone — Document de passation (pour nouvelle discussion)

Ce document sert à démarrer le projet **JGH Clone** dans une discussion dédiée. Il résume ce dont JGH Clone hérite de JGH Alert Bot, ce qu'il doit faire, et les décisions d'architecture à trancher au démarrage.

---

## 1. Ce qu'est JGH Clone

Un outil d'**automatisation du provisioning** pour Jaxaay Group : cloner, installer et restaurer des instances applicatives à la demande. Cas d'usage visés :

- **Cloner des instances** : Dolibarr, WordPress, Laravel, PHP/MySQL génériques
- **Installer des packs préfabriqués** : images/templates prêts à l'emploi (par secteur : fastfood, BTP, etc. — lien avec YessalERP)
- **Restaurer** des instances depuis une sauvegarde
- À terme : orchestration du déploiement client (lien avec YessalERP / JG Hosting)

C'est le successeur assumé du pipeline n8n historique (clonage Dolibarr ISPConfig→CloudPanel via SSH/clpctl/sed), documenté dans `prompt_clone_dolibarr_par_n8n.txt`, que JGH Clone doit remplacer par quelque chose de plus robuste et maintenable.

---

## 2. Ce que JGH Clone hérite de JGH Alert Bot

JGH Alert Bot (v1.18.0, opérationnel) fournit des briques réutilisables :

### Patterns d'architecture éprouvés
- **Contrôle-plan / données-plan** : Laravel/Python orchestre, les systèmes externes exécutent
- **Connexion DuckDB unique stable** + schéma versionné
- **Collecte SSH avec sentinelles** (`JGH_JSON_START…END`) pour un parsing robuste même si stdout est pollué (MOTD, warnings)
- **Écritures sous confirmation** (file `pending_*` + boutons inline Telegram ✅/❌) — réutilisable pour toute action sensible (un clonage est une action destructive/coûteuse : même circuit de confirmation)
- **Clé SSH à commande forcée** (lecture seule) — à adapter pour JGH Clone qui, lui, devra écrire/exécuter sur les serveurs cibles
- **Connecteurs par compte OVH** (multi-comptes, CK par compte)

### Intégrations déjà codées et testées
- **Client OVH** (`integrations/ovh_client.py`) : VPS, domaines, emails, hébergements, `serviceInfos`. Réutilisable pour provisionner de nouveaux services OVH (commande de domaine, création VPS…).
- **Client Dolibarr** (`integrations/dolibarr_client.py`) : lecture/écriture REST avec allowlist/denylist. Pour créer projet/tiers/contrat à la volée lors d'un clonage.
- **Client Telegram** : interface de commande et confirmation.
- **Miroirs Google Sheets/Agenda** : pour journaliser les opérations de clonage.

### Infrastructure partagée
- VPS bot `57.131.27.63` (CloudPanel), Dolibarr `51.75.121.166`, fleet OVH multi-comptes
- Convention de nommage VPS : `vps-xxxx-CLIENT_pack_annee`
- Règle d'ownership stricte (JG Hosting/YessalERP) : **WooCommerce (commercial) → Laravel (technique) → Dolibarr (compta)**, unidirectionnel

---

## 3. Le pipeline de clonage historique (à moderniser)

Le fichier `prompt_clone_dolibarr_par_n8n.txt` décrit le pipeline n8n existant pour cloner Dolibarr (`pos.yessal.com` ISPConfig → nouvelle instance CloudPanel). Étapes clés à reprendre/améliorer :

1. **Déclencheur** (manuel pour test, puis WooCommerce/API)
2. **Dump source** : `mysqldump` + archive `.tar.gz` (htdocs + documents) vers `/tmp`
3. **Transport** : les serveurs ne communiquent pas directement en SSH → passage par l'orchestrateur (n8n centralise, ou Coolify/nouveau système)
4. **Provisioning cible** : `clpctl` (CloudPanel) pour créer site + base
5. **Restauration** : extraction fichiers + import SQL
6. **« Perfect match »** : `sed -i` sur `conf.php` (URL, chemins, identifiants BDD)
7. **Finalisation** : `chown` récursif + `clpctl lets-encrypt:install` (SSL)

**Leçons du pipeline n8n** (à ne pas réapprendre) :
- Parsing JSON n8n fragile → **sentinelles obligatoires** dans les scripts bash (le même pattern que JGH Alert Bot)
- SSH direct entre serveurs échoue (`Permission denied / publickey`) → orchestration centralisée
- Transport SFTP nécessaire (les deux serveurs ne se parlent pas)

---

## 4. Décisions d'architecture à trancher au démarrage

Ces questions sont à débattre en début de discussion JGH Clone :

1. **Orchestrateur** : rester sur n8n ? Passer à **Coolify** (API REST versionnée, recommandé dans les notes YessalERP) ? Ou un orchestrateur Python/Laravel maison (cohérent avec JGH Alert Bot) ?
   - Note existante : Coolify est positionné comme exécuteur principal pour le fleet Dolibarr client ; CloudPanel en remplacement d'ISPConfig pour l'hébergement classique périphérique.

2. **Golden images versionnées** (par secteur) au lieu de cloner des instances live — prévu pour YessalERP. JGH Clone doit-il gérer ces images ?

3. **Interface** : commandes Telegram (comme Alert Bot) ? Interface Laravel/Filament ? API appelée par WooCommerce ?

4. **Périmètre initial** : commencer par **Dolibarr uniquement** (le cas le plus mûr, pipeline documenté) puis étendre à WordPress/Laravel/PHP ? Ou concevoir générique d'emblée ?

5. **Sécurité des exécutions** : JGH Clone écrit et exécute sur des serveurs (contrairement à Alert Bot lecture seule). Comment sécuriser (confirmation, dry-run, rollback) ?

---

## 5. Documents à joindre à la nouvelle discussion

Pour démarrer JGH Clone efficacement, joindre :

1. **`JGH_Clone_Passation.md`** (ce document)
2. **`prompt_clone_dolibarr_par_n8n.txt`** — le pipeline de clonage détaillé existant
3. **`JGH_Alert_Bot_Fonctionnalites.md`** — pour connaître les briques réutilisables
4. **`JGH_Alert_Bot_Architecture.md`** (doc existant du projet Alert Bot) — patterns détaillés
5. Idéalement, un **extrait du code** des clients réutilisables (`ovh_client.py`, `dolibarr_client.py`, le pattern SSH+sentinelles de `ssh_collector.py`)

---

## 6. Contexte Jaxaay Group (rappel)

- **YessalERP** : SaaS de déploiement d'instances Dolibarr sectorisées (Laravel control plane + Coolify/Docker data plane). JGH Clone est un maillon de ce projet.
- **JG Hosting** : plateforme d'hébergement (domaines, CloudPanel, email pro) façon OVH, WooCommerce comme couche commerciale, manager Laravel client.
- **JGH Alert Bot** : supervision (ce projet, opérationnel).
- Devise : **FCFA/XOF**. Paiements ouest-africains (Wave, Orange Money, CinetPay, PayTech) : connecteurs custom obligatoires.
- Développement en **français**. Windows/PowerShell local, déploiement VPS Linux via git.

---

## 7. Principe de réutilisation

**Objectif affiché** : JGH Clone doit s'inspirer et réutiliser JGH Alert Bot autant que possible (mêmes patterns, mêmes clients d'intégration, même rigueur : versioning incrémental, confirmation des actions sensibles, sentinelles SSH, secrets hors repo). Ne pas réinventer ce qui marche déjà.
