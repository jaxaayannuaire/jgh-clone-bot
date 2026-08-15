# JG Hosting & YessalERP — Synthèse du projet

**Jaxaay Group — août 2026**
Document de synthèse des décisions d'architecture, d'infrastructure et de modèle
économique. Fait suite au socle livré (`GUIDE_LOT1.md`).

---

## 1. Ce qu'on construit

Une plateforme d'hébergement et de SaaS ERP pour PME et associations
ouest-africaines, en deux volets qui partagent la même infrastructure :

| Volet | Contenu |
|---|---|
| **JG Hosting** | Noms de domaine, hébergement web, email pro — façon OVH, en local |
| **YessalERP** | Instances Dolibarr sectorisées prêtes à l'emploi (Tambali, POS, Asso, Pro) |
| **JGH Alert Bot** | Supervision de l'ensemble (existant, v1.18.0, opérationnel) |

Devise FCFA/XOF. Développement, documentation et interfaces en français.

---

## 2. Décisions d'architecture

### 2.1 Répartition des rôles

| Brique | Rôle | Statut |
|---|---|---|
| **WooCommerce + Subscriptions** | Commande, abonnement, paiement, identité client | Retenu, phase 1 |
| **Laravel Manager** | Espace client technique, provisioning, état de l'infrastructure | En construction |
| **Dolibarr (ERP interne)** | Tiers, contrats, factures définitives | Existant |
| **Coolify** | Exécution de la flotte Dolibarr client | Retenu |
| **CloudPanel** | Hébergement classique JG Hosting (WordPress, PHP/MySQL) | Conservé, périmètre réduit |
| **JGH Alert Bot** | Supervision, échéances, alertes | Opérationnel |

### 2.2 Règle d'ownership — non négociable

```
   WooCommerce  ──────►  Laravel Manager  ──────►  Dolibarr
   (commerce)             (technique)              (comptabilité)
```

Flux **strictement unidirectionnel**. Chaque donnée a une seule source de
vérité : l'identité et l'abonnement dans WordPress, l'instance et ses
credentials dans Laravel, la facture dans Dolibarr. Le manager n'a ni
inscription, ni reset de mot de passe — leur apparition signalerait une dérive.

Conséquence pour le bot : `expires_on` vient de Woo Subscriptions pour les
clients JG Hosting (`source_of_expiry='woo'`), des contrats Dolibarr pour les
clients historiques. Deux horloges divergeraient au premier renouvellement
anticipé.

### 2.3 Identité — SSO par ticket, Keycloak écarté

**Keycloak (et tout IdP) écarté à ce stade.** Justifié uniquement pour
centraliser les accès aux outils internes, mais la couverture serait incomplète :
n8n réserve le SSO à ses offres payantes, CloudPanel n'en a pas. Un IdP qui
laisse dehors deux outils critiques ne vaut pas son coût d'exploitation.
À reconsidérer quand YessalERP aura assez de tenants.

**Retenu : ticket à usage unique + vérification back-channel.**

```
1. Client connecté sur jghosting.sn → « Espace technique »
2. WordPress  → ticket (32 octets, transient 60 s), redirection vers Laravel
3. Laravel    → POST wp-json/jgh/v1/sso-verify  [serveur à serveur, secret partagé]
4. WordPress  → brûle le ticket, renvoie l'identité
5. Laravel    → miroir User, Auth::login, session régénérée
```

Aucune donnée d'identité ne transite par le navigateur. Tout passe par
`App\Contracts\IdentityProvider` : basculer vers OIDC plus tard ne touchera
qu'une implémentation.

**Limite assumée** : pas de déconnexion globale. Parade — session de 2 h et
logout enchaîné vers WordPress.

### 2.4 Coolify plutôt que CloudPanel pour Dolibarr

La raison décisive n'est pas l'API, c'est que **l'image Docker officielle
Dolibarr se configure par variables d'environnement** (`DOLI_DB_*`,
`DOLI_URL_ROOT`, `DOLI_ENABLE_MODULES`). Toute l'étape `sed -i` sur `conf.php`
du pipeline n8n — sa partie la plus fragile — disparaît.

Effets en chaîne :
- **Le pack devient un artefact versionné** : tag d'image + liste de modules +
  SQL de seed monté dans `docker-init.d`. Plus d'instance de référence vivante
  qui dérive silencieusement.
- **Les mises à jour deviennent tenables** : changer un tag avec
  `DOLI_INSTALL_AUTO=1`, au lieu de 30 passages manuels par `/install`.
- **API versionnée** au lieu de parsing de stdout SSH.

**Le workflow n8n n'est pas perdu** : il devient la voie de *migration* des
clients existants vers les conteneurs, pas la voie de provisioning.

**Règle à tenir absolument** : une seule image pour toute la flotte, les modules
métier activés par variable d'environnement et le code maison monté en volume
`custom/`. Un client = une configuration, **jamais** une image dédiée — sinon on
perd exactement ce que le conteneur apportait.

---

## 3. Infrastructure

### 3.1 Trois VPS au démarrage

| | Rôle | Dimensionnement |
|---|---|---|
| **VPS 1** | WordPress/Woo + Laravel Manager + MySQL | 4 vCPU / 8 Go / 80 Go NVMe |
| **VPS 2** | Coolify (plan de contrôle) | 2 vCPU / 4 Go / 40 Go |
| **VPS 3** | Plan de données — flotte Dolibarr | 6 vCPU / 12 Go / 160 Go NVMe |

VPS 2 est petit mais **séparé pour raison de sécurité** : Coolify détient les
clés SSH de toute la production, il n'a rien à faire sur la même machine qu'un
WordPress public.

VPS 3 est le seul à **scaler horizontalement** : au client suivant, on ajoute un
serveur dans Coolify, on ne migre rien.

**Rien pour l'email pro** (revente OVH), **rien pour les sauvegardes**
(Rclone + Google Drive chiffré, existant). **Aucun client sur le VPS
d'exploitation** (bot + n8n) : le jour d'une saturation disque causée par un
client, le système d'alerte doit être précisément ce qui ne tombe pas.

*Démarrage à deux VPS possible* en fusionnant 2 et 3. Déclencheur de
séparation : 10 instances clients, ou l'ajout d'un second serveur de charge.

### 3.2 Ce qu'on installe sur VPS 3

Le minimum — Coolify installe Docker et Traefik lui-même.

- Debian 13 / Ubuntu 24.04 minimal, **aucun panel**, rien sur les ports 80/443
- Utilisateur d'accès + clé publique Coolify
- UFW : SSH restreint à VPS 2 et au VPS du bot, 80/443 ouverts
  → **piège** : Docker contourne UFW dès qu'un conteneur publie un port. Aucun
  port publié, tout par Traefik. MariaDB n'expose jamais 3306.
- MariaDB partagée **déployée comme ressource Coolify** (une base par tenant),
  `innodb_buffer_pool_size` ≈ 50 % de la RAM allouée
- Clé de collecte JGH (`command=`, `no-pty`) + métrique `docker system df` —
  sur un plan de données, ce sont les images et volumes orphelins qui remplissent
  le disque
- Sauvegardes par tenant : dump de sa base + archive de son volume `documents`.
  Sauvegarder `/var/lib/docker` n'a aucun intérêt.

**Réflexe** : aucun `apt install` ni `docker run` manuel sur VPS 3. Le jour où
ça arrive, il cesse d'être reproductible.

### 3.3 Capacité de VPS 3

**25 à 35 instances confortablement, 40 en poussant.** La RAM est le seul
facteur limitant.

| Poste | RAM |
|---|---|
| Système + Docker + agent Coolify | ~1 Go |
| Traefik | ~150 Mo |
| MariaDB partagée | ~3,5 Go |
| **Disponible pour les conteneurs** | **~7 Go** |

Un conteneur Dolibarr : 120–150 Mo au repos, 250–350 Mo en charge. À ~200 Mo
moyens, la trentaine tient avec de la marge.

Disque et CPU ne contraignent pas : l'image est mutualisée, une base
d'association pèse 50–200 Mo, `documents` rarement plus de 500 Mo — ~50 Go sur
160 à 30 instances. 6 cœurs absorbent 60–100 utilisateurs dont 10 simultanés.

**Levier** : sortir MariaDB sur un VPS séparé libère 3,5 Go → ~50 instances.
À faire en approchant 30, pas avant.

---

## 4. Parcours client — commande à première connexion

1. **Commande Woo** — abonnement, champs personnalisés (organisation,
   sous-domaine, email admin). Sous-domaine vérifié en AJAX et **réservé avant
   paiement** (5 min).
2. **Paiement** — déclencheur `order.completed`, **jamais** `order.created` :
   le mobile money passe souvent par `on-hold`.
3. **Webhook → Laravel** — idempotence par `woo_order_id` (Woo rejoue ses
   webhooks). Création du `User` miroir et du `Service` en `pending`.
4. **Provisioning (~2–4 min)** — base + utilisateur MariaDB dédiés, application
   Coolify depuis le tag du pack, variables `DOLI_*`, volume de seed monté sur
   `docker-init.d`, déploiement, certificat Traefik. *Le DNS n'est pas une étape :
   wildcard `*.yessalerp.com` posé une fois.*
5. **Injection client** — le pack est figé et versionné (SQL de seed) ; les
   données du client passent par l'**API REST Dolibarr** après démarrage
   (raison sociale, devise XOF, utilisateur admin du client).
   → Deux comptes coexistent : technique JG Hosting + client. **À déclarer dans
   les CGU** — un accès admin non déclaré sur des données comptables est un
   problème contractuel.
6. **Activation** — statut `active`, `expires_on` repris de Woo, push vers
   Dolibarr ERP, notification Telegram, email Brevo.
   **Jamais de mot de passe par email** : lien signé 48 h, le client le choisit.
7. **Première connexion** — jghosting.sn → « Espace technique » → SSO → manager.

**Échec** : `status=failed`, sortie exacte conservée dans `provisioning_jobs`,
alerte Telegram, job rejouable sur sa clé d'idempotence. Côté client, la page de
suivi prend la parole plutôt que de laisser un silence.

---

## 5. Page de suivi d'installation (livrée)

Quatre jalons : commande validée → paiement reçu → installation en cours
(sous-étapes réelles) → prêt / erreur.

- **Aucune barre de pourcentage.** Les sous-étapes sont des lignes réelles de
  `provisioning_jobs`. Une progression simulée qui avance pendant qu'un
  déploiement est bloqué est pire que pas de progression.
- **Lien signé** pour l'accès juste après paiement (pas de session encore), route
  authentifiée pour y revenir plus tard.
- **Le sondage s'arrête** dès l'état terminal — forfait mobile.
- **État « bloqué »** au-delà de 10 min sans issue.
- Message d'échec précisant que **l'abonnement ne démarre qu'à l'aboutissement**.

---

## 6. Modèle économique

### 6.1 Grille tarifaire

| Pack | Année 1 | Années suivantes |
|---|---|---|
| Tambali | 15 000 | 10 000 |
| POS | 36 000 | 25 000 |
| Asso | 50 000 | 40 000 |
| Pro | 75 000 | 50 000 |

*Prix annuels, FCFA.*

### 6.2 À 30 instances (mix 12 Tambali / 9 POS / 6 Asso / 3 Pro)

| | Année 1 | Renouvellement |
|---|---|---|
| Chiffre d'affaires | **1 029 000** | **735 000** |
| Infrastructure (3 VPS, ~40 €/mois) | 375 000 | 375 000 |
| **Reste avant travail** | 654 000 | **360 000** |

En régime de renouvellement, l'infrastructure absorbe **la moitié du chiffre** —
soit ~30 000 FCFA/mois restants avant la moindre heure de travail.

Seuils : **~16 clients** couvrent l'infrastructure · **~57** dégagent
1 000 000/an net · **~140** approchent 3 000 000/an.

### 6.3 Le point structurel à corriger

**Le revenu par client baisse de 20 à 33 % en année 2, alors que le coût de
support ne baisse pas dans les mêmes proportions.** C'est l'inverse d'un modèle
sain, où le client fidèle est le plus rentable.

À 140 clients, le support seul (30 min/mois chacun) représente 70 h mensuelles :
**grille tarifaire de volume, modèle de service haut contact.** C'est là que ça
coince — pas dans le nombre d'instances par VPS.

Trois pistes, par impact décroissant :
1. **Inverser le renouvellement** — année 1 plus chère (elle inclut le
   paramétrage), puis prix stable. Pas dégressif.
2. **Facturer ce qui coûte** — support, formation, reprise de données, sur-mesure.
3. **Revoir le Pro** — 75 000 FCFA/an pour un ERP complet hébergé et supporté est
   en dessous du coût d'un seul poste chez la plupart des concurrents.

### 6.4 Variables plus déterminantes que la marge unitaire

- **Taux d'encaissement** — sans prélèvement récurrent tokenisé (le mobile money
  ne le permet généralement pas), le renouvellement est une relance manuelle
  annuelle. Écart réaliste de 15 % sur le revenu, à coût constant. **Plus grosse
  variable du modèle.**
- **Coût de support** — 0,5 à 2 h/mois/client la première année. Le vrai coût
  variable est humain, pas serveur.
- **Churn des six premiers mois** — un ERP mal démarré est abandonné avant d'être
  adopté. L'onboarding vaut plus que toute optimisation technique.

---

## 7. Services additionnels — stratégie de produit d'appel

L'hébergement bas prix est assumé comme produit d'appel. **Condition de
viabilité : que la base coûte quasiment rien à servir.** Industrialiser le
support passe donc *avant* d'ajouter des services — sinon on vend des options à
des clients déficitaires en base.

| Service | Priorité | Note |
|---|---|---|
| **Relance factures** | **1 — commencer ici** | C'est le moteur J-60→J-1 du bot pointé sur des échéances de facture. Brevo, idempotence, rattrapage : la machine existe. Coût marginal ~nul, touche la douleur n°1 des PME. |
| Email pro | 2 | Revente OVH, marge faible. Sa valeur est la **rétention**, pas le profit. |
| Modules métier (chantier, immobilier, GRH) | 3 | Sûrs, différenciants, réutilisables. |
| Agent IA | 4 | **Seul service à coût variable.** Compteur par tenant dès la 1re ligne de code, quota inclus, dépassement facturé — sinon vendu à perte sans le voir. |
| **Paie** | **Différer** | IPRES, CSS, IPM, barème IR : produit **réglementé**, exposition contractuelle en cas d'erreur, veille annuelle obligatoire. Nécessite un partenariat avec un cabinet comptable qui porte la responsabilité. |

---

## 8. Feuille de route

| Lot | Contenu | État |
|---|---|---|
| **1 — Socle & Identité** | SSO Woo↔Laravel, modèle `services`, contrats de connecteurs | ✅ livré |
| **1b — Suivi d'installation** | Page Livewire, 4 jalons, lien signé, activation | ✅ livré |
| 2 — Provisioning | `CoolifyConnector`, webhook `order.completed`, `provisioning_jobs` | à faire |
| 3 — Domaines | `RegistrarConnector` OVH + Netim, disponibilité, éditeur DNS | à faire |
| 4 — Email pro | Revente OVH (endpoint `/email/pro` déjà au périmètre du bot) | à faire |
| 5 — Cycle de vie | Suspension, grâce, résiliation, restitution, jonction bot JGH | à faire |

---

## 9. Points ouverts

1. **Paiement récurrent tokenisé** — à confirmer auprès de PayDunya / CinetPay /
   Wave / Orange Money. Si aucun ne le supporte, Woo Subscriptions bascule en
   renouvellement manuel : la relance et la suspension deviennent le **chemin
   nominal**, pas l'exception. Impact direct sur le lot 5 et sur le modèle.
2. **Industrialisation du support** — prérequis à l'ajout de services, pas une
   tâche parallèle.
3. **Repositionnement tarifaire** — le renouvellement dégressif est le principal
   frein structurel identifié.
4. **Golden images sectorielles** — figer le format d'artefact (tag + modules +
   seed SQL) avant d'écrire le lot 2.
