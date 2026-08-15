# CloudPanel vs Coolify — comparaison détaillée pour YessalERP

*Version 1.0 — Juillet 2026*
*Complément de l'« Étude complète de Coolify — orientée YessalERP » (v1.0).*
*Contexte : migration envisagée ISPConfig → CloudPanel, et évaluation parallèle de Coolify comme exécuteur de provisioning.*

---

## 0. L'avertissement à lire en premier : deux catégories différentes

La comparaison n'est pas « produit A contre produit B dans le même rôle ». Ce sont deux paradigmes :

| | **CloudPanel** | **Coolify** |
|---|---|---|
| Nature | Panel de contrôle serveur classique | PaaS conteneurisée auto-hébergée |
| Remplace | ISPConfig (même paradigme) | Ta couche de provisioning bas-niveau |
| Exécute les apps comme | Processus sur l'hôte (Nginx + PHP-FPM), sous un *site user* système | Conteneurs Docker isolés |
| Pilotage | CLI `clpctl` en SSH | API REST `/api/v1` à jetons |
| Analogie | « cPanel léger et moderne » | « Heroku que tu héberges » |

**Conséquence** : ta décision ISPConfig → CloudPanel est saine *si* tu restes dans le modèle « site classique + clonage d'instance vivante ». Mais elle ne t'apporte quasiment rien sur les trois points de douleur identifiés dans l'étude Coolify (transport de fichiers entre serveurs, réécriture `sed` de `conf.php`, absence d'API d'orchestration). La vraie question stratégique est donc :

> **Reste-je dans le paradigme panel classique (ISPConfig → CloudPanel), ou passe-je au paradigme conteneur (Coolify) pour la flotte d'instances client ?**

Les deux ne s'excluent pas : voir §12 (scénario de coexistence).

---

## 1. État des produits (mi-2026)

### CloudPanel
- **Licence** : gratuit, open-source, livré en paquet Debian (`.deb`).
- **Version** : branche v2.5.x. v2.5.0 (nov. 2025) a apporté le support PHP 8.4 et MariaDB 11.4 ; le changelog courant est à v2.5.4 (1er juillet 2026). Rythme de releases régulier.
- **Stack** : Nginx, PHP (multi-versions), Node.js, Python, sites statiques, reverse proxy ; bases MySQL / MariaDB / Redis.
- **Empreinte** : très léger (fonctionne sur ~2 Go RAM / 1 cœur).
- **Sécurité récente** : v2.5.0 a corrigé **deux failles HIGH d'élévation de privilèges** (via `clpctlWrapper` ; et *site user* pouvant changer des mots de passe Linux). Le patch management est donc requis, comme partout.

### Coolify
- **Licence** : open-source, Apache 2.0.
- **Version** : v4.0.0 stable (avr. 2026, après ~2 ans de bêta), v4.1.0 (mai 2026 : audit logging structuré, build pack Railpack, serveur MCP). v5 en planification sans timeline.
- **Stack** : Docker (+ Traefik ou Caddy), déploiement depuis Git / Dockerfile / image / Compose ; bases sur 8 moteurs (PostgreSQL, MySQL, MariaDB, MongoDB, Redis, ClickHouse, Dragonfly, KeyDB).
- **Empreinte** : plus lourde (le dashboard + Docker consomment ; on recommande un serveur dédié à Coolify + N serveurs de ressources).
- **Sécurité récente** : plusieurs CVE critiques en 2025/2026 (dont CVE-2026-31431). Patch management obligatoire.

---

## 2. Automatisation et API — LE différenciateur pour YessalERP

C'est le point le plus structurant pour une plateforme dont le control plane Laravel doit tout piloter.

### CloudPanel : CLI `clpctl` en SSH, pas d'API REST native
CloudPanel **n'a pas d'API REST officielle**. Toute l'automatisation passe par le binaire `clpctl` exécuté en SSH. Une API REST native est une demande ouverte de la communauté depuis longtemps, non livrée à ce jour. Des wrappers tiers existent (ex. un SDK Node.js communautaire qui enveloppe `clpctl` via SSH et renvoie `{ command, success, stdout, stderr, code }`), mais :
- ils s'appuient sur une **connexion SSH root/site-user** (mot de passe ou clé) — c'est-à-dire précisément le canal SSH dont tu constates la fragilité dans ton workflow actuel ;
- tu restes dépendant du **parsing de stdout** (ton chantier n8n « Parse Chemins Source » et tes sentinelles `PATHS_JSON_START…END` sont exactement la rançon de ce modèle) ;
- pas de contrat d'API versionné, pas de gestion fine de permissions par jeton.

Commandes `clpctl` typiques pour provisionner une instance :
```bash
clpctl site:add:php --domainName=client1pos.yessalerp.com \
  --phpVersion=8.4 --vhostTemplate='Generic' \
  --siteUser=client1posuser --siteUserPassword='...'
clpctl db:add --domainName=client1pos.yessalerp.com \
  --databaseName=doli_client1 --databaseUserName=... --databaseUserPassword='...'
clpctl db:import --databaseName=doli_client1 --file=dump.sql.gz
clpctl lets-encrypt:install:certificate --domainName=client1pos.yessalerp.com
clpctl system:permissions:reset --directories=770 --files=660 --path=.
```
On reconnaît ligne pour ligne la logique de ton prompt de clonage. CloudPanel **industrialise proprement** ce que tu fais déjà — mais ne change pas le paradigme.

### Coolify : API REST `/api/v1` à jetons
- API REST versionnée, documentée en OpenAPI 3.1, jetons Laravel Sanctum **scopés par équipe** avec permissions granulaires (`read` / `write` / `deploy`).
- Ton `CoolifyConnector` appelle du HTTPS structuré, avec réponses JSON typées et gestion de `429`/`Retry-After` — plus de parsing de stdout fragile.
- Coolify parle SSH à *ses* serveurs cibles : le SSH redevient un détail interne géré par l'outil, plus un canal que ton control plane doit orchestrer.

**Verdict §2** : pour une architecture « control plane API-first », CloudPanel te maintient dans le CLI-over-SSH (le monde que tu cherches à fiabiliser), Coolify te donne l'API que ton design réclame.

---

## 3. Isolation multi-tenant

| | CloudPanel | Coolify |
|---|---|---|
| Mécanisme | 1 *site user* système par site (isolation au niveau OS) | 1 réseau Docker par stack Compose (isolation conteneur) |
| Portée | Séparation des fichiers/permissions ; MySQL partagé sur l'hôte | Conteneurs + réseau ; base embarquée dans le stack du client |
| Angle mort | Optimisé pour hardening système, **pas pensé pour l'isolation multi-tenant forte** ; toutes les bases vivent sur le même serveur MySQL | Réseau **plat par destination** par défaut (risque de lateral movement) → à corriger par « 1 client = 1 stack auto-suffisant » |

Les deux exigent de la rigueur, mais la nature de l'isolation diffère : CloudPanel isole des *utilisateurs sur une même machine* ; Coolify isole des *conteneurs sur un même réseau*. Pour du SaaS où chaque client doit être étanche (y compris sa base), le modèle conteneur bien configuré (§7 de l'étude Coolify) offre une frontière plus nette qu'un serveur MySQL mutualisé.

---

## 4. Provisioning d'une instance Dolibarr — pas à pas comparé

| Étape | CloudPanel (`clpctl` SSH) | Coolify (API REST) |
|---|---|---|
| Créer le site | `clpctl site:add:php` | `POST /applications` ou stack Compose |
| Créer la base | `clpctl db:add` | Base incluse dans le stack Compose |
| Transporter les fichiers | `tar.gz` + SFTP via n8n (serveurs hétérogènes) | Aucun : image Docker taguée |
| Importer la base | `clpctl db:import dump.sql.gz` | Seed au premier boot / dump init |
| Régler `conf.php` | `sed -i` (URL, chemins, DB) — **fragile** | Injection env `DOLI_*` — **idempotent** |
| Permissions | `clpctl system:permissions:reset` + `chown` | Géré par le conteneur |
| SSL | `clpctl lets-encrypt:install:certificate` | Automatique dès domaine attaché |

CloudPanel raccourcit et fiabilise chaque commande individuelle, mais **conserve toute la chaîne** (transport, import, `sed`). Coolify **supprime des étapes entières** en changeant la représentation du pack (artefact versionné plutôt que clone d'instance vivante).

---

## 5. Versionnement des golden packs

- **CloudPanel** : reste sur ton modèle actuel — « Publish stable version » = dump SQL + archive fichiers, réimportés à chaque nouveau client. Simple, mais l'état de référence est une *instance vivante* qu'on copie.
- **Coolify** : le pack devient une **image Docker taguée** (`yessalerp/dolibarr-fastfood:1.2`) + seed. Reproductible, testable en CI, sans transport. C'est plus aligné avec ta vision « versionnable et clonable ».

---

## 6. SSL, backups, DNS

- **SSL** : les deux gèrent Let's Encrypt en automatique avec renouvellement. Égalité.
- **Backups** : CloudPanel fait des backups `mysqldump` + remote S3 ; Coolify fait des backups DB planifiés vers S3 en quelques clics. Dans les deux cas, ta stratégie rclone + Google Drive + `rclone crypt` reste un complément off-site chiffré légitime.
- **DNS / e-mail** : ni l'un ni l'autre n'est un serveur DNS/mail complet (CloudPanel ne bundle pas l'e-mail ; Coolify non plus). Pas de régression par rapport à ce que tu gères déjà par ailleurs.

---

## 7. Ressources, exploitation, courbe

| | CloudPanel | Coolify |
|---|---|---|
| Empreinte serveur | Très légère (~2 Go RAM) | Plus lourde (Docker + dashboard) |
| Modèle recommandé | Panel sur le serveur qui héberge les sites | 1 serveur Coolify dédié + N serveurs de ressources |
| Compétence requise | Admin Linux classique (Nginx, PHP-FPM, MySQL) | Docker / Compose / réseau conteneur |
| Debug | Logs Nginx/PHP sur l'hôte | Logs conteneur + réseau Docker |
| Familiarité YessalERP | Élevée (proche d'ISPConfig) | Nouvelle brique à apprendre |

CloudPanel gagne sur la légèreté et la proximité avec ce que tu maîtrises déjà. Coolify demande un investissement Docker mais te rapproche de ton architecture cible.

---

## 8. Sécurité — égalité en vigilance requise

Les deux ont eu des CVE HIGH/critiques récentes (CloudPanel : élévations de privilèges corrigées en v2.5.0 ; Coolify : CVE-2026-31431 et autres). Aucun des deux n'est « à installer et oublier ». Différence de surface : CloudPanel expose un panel + SSH root/site-user ; Coolify expose un dashboard + une API à jetons. Dans les deux cas : firewall strict, dashboard non ouvert au monde, secrets chiffrés, rotation, patching.

---

## 9. Tableau de synthèse pondéré (critères YessalERP)

Notation indicative de 1 (faible) à 5 (fort), du point de vue des besoins YessalERP.

| Critère | Poids | CloudPanel | Coolify |
|---|---|---|---|
| API d'orchestration pour control plane Laravel | ⭐⭐⭐ | 2 (CLI SSH, pas d'API native) | 5 (REST à jetons) |
| Suppression des frictions actuelles (transport, `sed`) | ⭐⭐⭐ | 2 | 5 |
| Isolation multi-tenant par client | ⭐⭐⭐ | 3 (user système, MySQL partagé) | 4 (réseau conteneur, si bien configuré) |
| Versionnement des golden packs | ⭐⭐ | 3 (dump+archive) | 5 (image taguée) |
| Légèreté / coût serveur | ⭐⭐ | 5 | 3 |
| Proximité avec les compétences actuelles | ⭐⭐ | 5 | 3 |
| Remplacement direct d'ISPConfig | ⭐ | 5 | 2 (paradigme différent) |
| Maturité / stabilité | ⭐⭐ | 4 | 4 |
| Effort de sécurisation | ⭐⭐ | 3 | 3 |

Lecture : **CloudPanel domine sur « continuité »** (léger, familier, remplaçant naturel d'ISPConfig). **Coolify domine sur « architecture cible »** (API, suppression des frictions, versionnement). Le choix dépend de ce que tu optimises : minimiser le changement, ou aligner l'infra sur ton design control/data plane.

---

## 10. Ce que chaque option implique pour ton chantier actuel

- **Si tu choisis CloudPanel** : ton workflow n8n de clonage (13 nœuds, sentinelles `PATHS_JSON_START…END`, détection PHP, `sed` sur `conf.php`) reste **pertinent et nécessaire** — tu le fiabilises, tu ne le supprimes pas. Le bug `Cannot read properties of null` du nœud « Parse Chemins Source » reste un problème de ta chaîne, parce que tu continues de parser du stdout SSH.
- **Si tu choisis Coolify** : une grande partie de ce chantier **devient obsolète** — plus de parsing de stdout pour les chemins, plus de `sed`, plus de transport SFTP. En contrepartie, tu réécris ton golden pack en image Docker et tu implémentes le `CoolifyConnector`.

---

## 11. Recommandation

Trois scénarios, du plus conservateur au plus transformateur.

**A. CloudPanel seul (continuité).**
Tu migres ISPConfig → CloudPanel, tu industrialises ton workflow n8n autour de `clpctl`. Le plus rapide à livrer, le moins risqué à court terme, mais tu conserves toutes les frictions structurelles et tu n'obtiens jamais l'API d'orchestration que ton architecture réclame. Choix raisonnable si la priorité absolue est de sortir vite d'ISPConfig avec un minimum de changement.

**B. Coolify seul (cible).**
Tu fais de Coolify l'exécuteur de provisioning derrière le `CoolifyConnector`, tu abandonnes le duo panel + clonage. Le plus aligné avec ton design, celui qui supprime le plus de dette. Coût : apprendre Docker/Coolify, repenser les packs, sécuriser un composant à CVE.

**C. Coexistence (pragmatique, recommandé).**
- **Coolify** pour la **flotte d'instances Dolibarr client** (le cœur du SaaS : isolation, API, packs versionnés).
- **CloudPanel** (ou rien) pour l'**hébergement classique annexe** si tu as des sites hors-conteneur à gérer.
- La migration ISPConfig → CloudPanel garde alors du sens uniquement pour ce périmètre annexe ; le cœur métier bascule sur Coolify.

Ma lecture : le point §2 (absence d'API REST native chez CloudPanel) est un signal fort. Ton architecture entière repose sur un control plane qui pilote des exécuteurs par API. CloudPanel te force à simuler cette API par du CLI-over-SSH — exactement le modèle dont proviennent tes frictions actuelles. Si le cœur de YessalERP est le déploiement automatisé d'instances, **le scénario C (avec Coolify comme moteur du SaaS) sert mieux ta vision** que de simplement remplacer un panel par un autre.

---

## 12. Question à trancher pour la suite

Avant de figer quoi que ce soit, une seule question détermine le reste :

> **Veux-tu que YessalERP reste une plateforme qui *clone des instances* (paradigme panel), ou qu'elle *matérialise des artefacts versionnés* (paradigme conteneur) ?**

- Réponse « clone » → CloudPanel suffit, migre et fiabilise n8n.
- Réponse « artefact » → Coolify est le bon moteur, CloudPanel devient accessoire.

La réponse conditionne le `CoolifyConnector`, le format des golden packs, et le sort de ton workflow n8n actuel.

---

*Sources : documentation officielle CloudPanel (cloudpanel.io/docs), releases GitHub cloudpanel-io, board de feature-requests CloudPanel (API REST non livrée), documentation et notes de version Coolify. Certains comparatifs tiers trouvés en ligne surestiment les capacités API de CloudPanel — cette étude s'appuie sur les sources primaires. Les deux produits évoluant vite, revalide les versions et l'état de la demande d'API REST CloudPanel avant décision finale.*
