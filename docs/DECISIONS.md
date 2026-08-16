# JGH Clone Bot — Journal des décisions

> Ce document consigne les **décisions d'architecture** et, surtout, les
> approches **explorées puis abandonnées**, avec leur justification. Il complète
> `JGH_Clone_Bot_Architecture.md` (qui, lui, ne décrit que l'état validé). Son but :
> qu'on ne retente pas une voie déjà écartée sans en connaître la raison.
>
> Format inspiré des ADR (Architecture Decision Records).

---

## D-001 — Stratégie de pack : deux images cuites (B), pas image commune + montage (A)

**Statut :** ✅ Adoptée (Stratégie B) — août 2026
**Remplace :** Stratégie A (abandonnée)

### Contexte

Un pack = un Dolibarr sectorisé (modules activés, code `custom/`, données de
seed, documents d'exemple). Il fallait décider **comment** assembler ces
éléments pour le déploiement conteneurisé.

### Option A — Image Dolibarr commune + code monté en volume (ABANDONNÉE)

L'idée initiale : une seule image `jgh/dolibarr:<version>` pour toute la flotte,
par-dessus laquelle on **monte** le code `custom/` du pack (volume depuis un tag
GitHub cloné sur l'hôte) et les documents (volume). Ce qui varie d'un pack à
l'autre (modules, code, seed) serait injecté au déploiement, pas cuit dans une
image.

**Argument séduisant :** le nombre d'images ≈ le nombre de versions de Dolibarr
(2–3), pas le nombre de packs (20). Un patch Dolibarr = un seul rebuild.

**Pourquoi c'est abandonné :** Coolify a un **bug connu sur le bind mount de
fichier** — monter un fichier unique crée un **dossier** à sa place au lieu de
monter le fichier. Le montage de code/config par fichier devient donc non
fiable. Contourner ce bug aurait demandé des acrobaties fragiles, à l'opposé de
l'objectif (sortir de la fragilité du pipeline n8n/`sed`).

### Option B — Deux images cuites par pack (ADOPTÉE)

Chaque pack = deux images publiées sur ghcr.io :
- `jgh-pack-<pack>` : Dolibarr + surcharges + `custom/` + documents (cuits)
- `jgh-pack-<pack>-db` : MariaDB + dump du pack

Aucun bind mount de fichier. Les images sont autonomes et reproductibles.

**Conséquences acceptées :**
- Plus d'images qu'en Stratégie A (deux par pack/version). Mitigé par le fait que
  les images de pack partagent les couches de l'image commune (ghcr.io ne stocke
  les couches qu'une fois).
- Un patch Dolibarr demande de reconstruire les images de pack — mais le
  processus est scripté (`build_pack_image.sh`) et reproductible.

**Ce que B nous a apporté, validé au test :** déploiement fiable de bout en bout,
sans dépendre d'un mécanisme de montage buggé. Le pack POS sort complet (données
+ documents + modules) d'un simple `docker compose` ou d'un déploiement Coolify.

---

## D-002 — Import du dump : par MariaDB (initdb.d), pas par les hooks Dolibarr

**Statut :** ✅ Adoptée

### Contexte

Le dump SQL du pack doit peupler la base au déploiement.

### Ce qui ne marchait pas

L'image Dolibarr a un mécanisme `docker-init.d` pour exécuter des scripts
post-installation. Mais **Dolibarr saute ce mécanisme quand la base est déjà
installée**. Or on veut justement une base déjà peuplée. Résultat : le dump
n'était pas correctement pris en compte (état incohérent 0/0/42).

### Décision

Placer le dump dans `/docker-entrypoint-initdb.d/` de l'image **MariaDB**.
MariaDB l'importe **à la création de la base**, avant que Dolibarr démarre.
Dolibarr trouve alors une base peuplée et saute proprement son installation
(« Schema update is not required … Enjoy ! »). Le conflit avec
`DOLI_INSTALL_AUTO` disparaît.

---

## D-003 — Documents : wrapper d'entrypoint, pas docker-init.d

**Statut :** ✅ Adoptée

Même cause que D-002 : `docker-init.d` étant sauté quand la base existe, les
documents ne pouvaient pas être injectés par ce biais. Solution : un **wrapper
d'entrypoint** (`jgh-entrypoint.sh`) cuit dans l'image, qui décompresse les
documents dans le volume `/var/www/documents` au premier boot (marqueur
`.jgh_documents_initialized` pour ne jamais écraser un client), puis chaîne vers
l'entrypoint officiel `docker-run.sh apache2-foreground`. Le wrapper s'exécute à
chaque démarrage, indépendamment de l'état de la base.

---

## D-004 — Encadrement du dump SQL (clés étrangères)

**Statut :** ✅ Adoptée

Le dump, avec ses tables en ordre alphabétique, provoquait des erreurs de clés
étrangères à l'import (1005 errno 150 : une table référence une table pas encore
créée). Décision : encadrer le dump par `SET FOREIGN_KEY_CHECKS=0` en tête et
réactivation + COMMIT en pied. Un tri topologique des dépendances (façon Akeeba)
serait sur-ingénierie pour notre contexte contrôlé.

---

## D-005 — Registre d'images : ghcr.io pour tout le projet JGH

**Statut :** ✅ Adoptée

Les images locales disparaissaient au nettoyage Docker/Coolify (volatilité
constatée trois fois). Décision : **GitHub Container Registry (`ghcr.io`)** pour
tout le développement des applications JGH (images + code + documentation au même
endroit). Images privées. Le self-hosted est réservé aux projets clients sur
mesure (souveraineté au cas par cas), pas au développement interne.

Validé : Coolify tire les images privées grâce au `docker login ghcr.io` de
l'hôte, sans configuration de registre supplémentaire.

---

## D-006 — Type d'instance : `client` par défaut (prudence)

**Statut :** ✅ Adoptée

Pour distinguer les instances jetables (test) des instances précieuses (client),
un champ `instance_type` gouverne le niveau de confirmation à la suppression.
Décision : **défaut `client`** (le plus prudent) ; une instance est traitée comme
précieuse sauf mention explicite `test` dans `/provision`. Conséquence : la
suppression d'une instance client exige la saisie de son nom exact (façon
Coolify), celle d'une instance test se fait par double confirmation à boutons.

---

## D-007 — Domaines : préfixe serveur `*.sN.yessalerp.com`

**Statut :** ✅ Adoptée (mise en œuvre SSL différée)

`yessalerp.com` est pris par le site commercial. Les instances clients vivent
sous un niveau **par serveur** : `*.s1.yessalerp.com` (s1 = serveur 1),
`*.s2…` pour un futur VPS, etc. Un seul wildcard DNS par serveur route tous ses
clients. Caractéristique assumée : le serveur d'un client est visible dans son
URL, et une migration inter-serveurs changerait l'URL. La génération SSL
(Let's Encrypt via Traefik) est différée à la phase domaines.
