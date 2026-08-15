#!/usr/bin/env python3
"""
publish_pack.py — Publication d'un pack Dolibarr JGH (YessalERP).

Transforme un environnement de référence (ex: pos.yessal.com) en artefact
déployable versionné :
    1. Dump SQL complet + purge des tables volatiles (sessions, logs, events)
    2. Export du custom/ (modules maison)
    3. Archive des documents (doctemplates, medias, données d'exemple fictives)
    4. Push vers le repo GitHub du pack (release taguée)
    5. Dépôt de l'archive documents

VERSION 1 : tourne SUR le serveur de référence (accès local à la base et aux
fichiers). Conçu pour être basculé plus tard en pilotage distant par le bot.

Les données d'exemple (société fictive, produits, factures démo) sont VOLONTAIRES
et CONSERVÉES — elles font partie de la valeur du pack. Seules les tables
volatiles sont purgées.

Usage :
    python3 publish_pack.py --pack pos --version 1.0.0 \\
        --doli-root /var/www/clients/client1/web11/web \\
        --changelog "Premiere version : caisse, stock, fidelite" \\
        [--dry-run]

Le push GitHub necessite GITHUB_TOKEN dans l'environnement ou --no-push.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Tables volatiles a purger systematiquement (validees contre la base POS reelle)
# On les VIDE meme si elles sont a 0 aujourd'hui : une base de reference en
# usage accumule sessions/logs avec le temps.
# ---------------------------------------------------------------------------

VOLATILE_TABLES = [
    "llx_session",
    "llx_events",
    "llx_event_element",
    "llx_notify",
    "llx_notify_def",
    "llx_notify_def_object",
    "llx_blockedlog",
    "llx_blockedlog_authority",
    "llx_commande_fournisseur_log",
    "llx_holiday_logs",
    "llx_product_customer_price_log",
    "llx_product_fournisseur_price_log",
    "llx_eventorganization_conferenceorboothattendee",
    "llx_eventorganization_conferenceorboothattendee_extrafields",
]

# Repos GitHub par pack (convention figee : un repo prive par pack)
PACK_REPOS = {
    "pos":     "jaxaayannuaire/jgh-pack-pos",
    "pro":     "jaxaayannuaire/jgh-pack-pro",
    "tambali": "jaxaayannuaire/jgh-pack-tambali",
    "asso":    "jaxaayannuaire/jgh-pack-asso",
    "immo":    "jaxaayannuaire/jgh-pack-immo",
}

# Dossiers de documents a exclure (caches/temp uniquement).
DOCS_EXCLUDE = {"admin/temp", "mycompany/logos/thumbs"}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Lecture de la config Dolibarr (conf.php)
# ---------------------------------------------------------------------------

def read_dolibarr_conf(doli_root: str) -> dict:
    """Extrait les parametres DB et le data_root depuis conf.php."""
    conf_path = Path(doli_root) / "conf" / "conf.php"
    if not conf_path.exists():
        sys.exit(f"conf.php introuvable : {conf_path}")

    conf = {}
    for line in conf_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        for key in ("db_host", "db_name", "db_user", "db_pass",
                    "db_port", "data_root"):
            needle = f"dolibarr_main_{key}"
            if needle in line and "=" in line:
                val = line.split("=", 1)[1].strip().strip(";").strip().strip("'\"")
                conf[key] = val
    conf.setdefault("db_host", "localhost")
    conf.setdefault("db_port", "3306")
    for required in ("db_name", "db_user", "data_root"):
        if not conf.get(required):
            sys.exit(f"Parametre manquant dans conf.php : dolibarr_main_{required}")
    return conf


# ---------------------------------------------------------------------------
# Etape 1 - Dump SQL avec purge des volatiles
# ---------------------------------------------------------------------------

def dump_database(conf: dict, out_sql: Path, dry_run: bool) -> None:
    """
    Produit un dump SQL de la base, avec les tables volatiles VIDEES.

    Methode : mysqldump complet, mais pour les tables volatiles on ne dumpe
    que la STRUCTURE (--no-data) - leur contenu est ainsi absent du pack,
    sans modifier la base de reference (non destructif).

    IMPORTANT (corrige erreur 1005 errno 150) : le dump est encadre par
    SET FOREIGN_KEY_CHECKS=0 / =1 sur TOUT le fichier. Sans ca, un CREATE TABLE
    avec une cle etrangere vers une table pas encore creee (ordre alphabetique)
    echoue. FOREIGN_KEY_CHECKS=0 permet de creer les tables dans n'importe quel
    ordre et de differer la verification des FK a la fin de l'import.
    On desactive aussi les checks uniques/AUTOCOMMIT pour un import fiable.
    """
    log(f"Dump de la base '{conf['db_name']}' -> {out_sql.name}")

    base_args = [
        "mysqldump",
        f"--host={conf['db_host']}",
        f"--port={conf.get('db_port', '3306')}",
        f"--user={conf['db_user']}",
        f"--password={conf.get('db_pass', '')}",
        "--single-transaction",
        "--default-character-set=utf8mb4",
        "--no-tablespaces",
        # Ne PAS laisser mysqldump ajouter ses propres SET par table ; on gere
        # l'encadrement global nous-memes (plus fiable pour l'import Dolibarr).
        "--skip-add-locks",
    ]

    if dry_run:
        log("[dry-run] mysqldump non execute")
        log(f"[dry-run] {len(VOLATILE_TABLES)} tables volatiles seraient videes "
            f"(structure gardee, donnees exclues)")
        return

    ignore_args = [f"--ignore-table={conf['db_name']}.{t}" for t in VOLATILE_TABLES]

    with open(out_sql, "w", encoding="utf-8") as f:
        # En-tete : desactive les verifications qui font echouer un import
        # dans une base ou l'ordre des tables et les FK posent probleme.
        f.write("-- JGH pack dump — en-tete d'import sur (FK checks off)\n")
        f.write("SET FOREIGN_KEY_CHECKS=0;\n")
        f.write("SET UNIQUE_CHECKS=0;\n")
        f.write("SET AUTOCOMMIT=0;\n")
        f.write("SET NAMES utf8mb4;\n")
        f.flush()

        full = base_args + ignore_args + [conf["db_name"]]
        log("Dump des donnees (hors tables volatiles)")
        subprocess.run(full, check=True, stdout=f)
        f.flush()

        structure = base_args + ["--no-data", conf["db_name"]] + VOLATILE_TABLES
        log("Ajout de la structure des tables volatiles (sans donnees)")
        subprocess.run(structure, check=True, stdout=f)
        f.flush()

        # Pied : reactive les verifications et valide la transaction.
        f.write("\n-- JGH pack dump — pied d'import (reactivation checks)\n")
        f.write("SET FOREIGN_KEY_CHECKS=1;\n")
        f.write("SET UNIQUE_CHECKS=1;\n")
        f.write("COMMIT;\n")
        f.write("SET AUTOCOMMIT=1;\n")

    size_mb = out_sql.stat().st_size / 1024 / 1024
    log(f"Dump termine : {out_sql.name} ({size_mb:.1f} Mo)")


# ---------------------------------------------------------------------------
# Etape 1bis - Nettoyage des references figees a l'environnement de reference
# ---------------------------------------------------------------------------

# Remplacements appliques sur le SQL APRES dump (jamais sur la base source).
# Cle = ce qu'on cherche, Valeur = ce qu'on met a la place.
#
# TAKEPOS_HEADER / TAKEPOS_FOOTER2 (logo, URL) : GARDES TELS QUELS (decision
# produit - donnees de demo assumees, cf. echange du 13/08/2026).
#
# fullpath_orig (llx_ecm_files) : champ documente comme informatif/historique
# (le champ fonctionnel est 'filepath', qui est deja relatif). On neutralise
# fullpath_orig par prudence, sans impact fonctionnel connu.
SQL_REFERENCE_CLEANUPS = [
    # Chemin absolu de l'instance de reference dans fullpath_orig (ecm_files)
    # et autres champs similaires -> vide (le champ n'est pas utilise pour
    # retrouver les fichiers, seul 'filepath' relatif l'est).
    ("/var/www/pos.yessal.com/web/documents", ""),
    ("/var/www/clients/client1/web11/web", ""),
]


def clean_sql_references(sql_path: Path, dry_run: bool) -> None:
    """
    Neutralise les chemins absolus figes de l'instance de reference dans le
    dump SQL (champs informatifs type fullpath_orig). N'affecte PAS les
    constantes TAKEPOS_HEADER/FOOTER2 (gardees intentionnellement).
    """
    log("Nettoyage des references a l'environnement de reference")
    if dry_run:
        log(f"[dry-run] {len(SQL_REFERENCE_CLEANUPS)} motif(s) seraient neutralises")
        return

    text = sql_path.read_text(encoding="utf-8", errors="ignore")
    total_hits = 0
    for needle, replacement in SQL_REFERENCE_CLEANUPS:
        count = text.count(needle)
        if count:
            text = text.replace(needle, replacement)
            total_hits += count
            log(f"  '{needle}' -> '{replacement}' ({count} occurrence(s))")
    sql_path.write_text(text, encoding="utf-8")
    log(f"Nettoyage termine : {total_hits} occurrence(s) neutralisee(s)")


# ---------------------------------------------------------------------------
# Etape 2 - Export du custom/
# ---------------------------------------------------------------------------

def archive_custom(doli_root: str, out_tar: Path, dry_run: bool) -> None:
    """Archive le dossier custom/ complet (modules maison)."""
    custom_dir = Path(doli_root) / "custom"
    if not custom_dir.exists():
        sys.exit(f"custom/ introuvable : {custom_dir}")

    log(f"Archivage de custom/ -> {out_tar.name}")
    if dry_run:
        n = sum(1 for _ in custom_dir.rglob("*") if _.is_file())
        log(f"[dry-run] {n} fichiers de custom/ seraient archives")
        return

    with tarfile.open(out_tar, "w:gz") as tar:
        tar.add(custom_dir, arcname="custom")
    size_mb = out_tar.stat().st_size / 1024 / 1024
    log(f"custom/ archive : {out_tar.name} ({size_mb:.1f} Mo)")


# ---------------------------------------------------------------------------
# Etape 3 - Archive des documents (donnees d'exemple incluses)
# ---------------------------------------------------------------------------

def archive_documents(conf: dict, out_tar: Path, dry_run: bool) -> None:
    """
    Archive le dossier documents (doctemplates, medias, ET donnees d'exemple
    fictives - elles font partie du pack). Exclut caches/temp et install.lock.
    """
    docs_dir = Path(conf["data_root"])
    if not docs_dir.exists():
        sys.exit(f"documents/ introuvable : {docs_dir}")

    log(f"Archivage des documents -> {out_tar.name}")
    if dry_run:
        size = sum(f.stat().st_size for f in docs_dir.rglob("*") if f.is_file())
        log(f"[dry-run] documents ~{size/1024/1024:.1f} Mo seraient archives "
            f"(hors caches/temp et install.lock)")
        return

    def _filter(tarinfo: tarfile.TarInfo):
        for excl in DOCS_EXCLUDE:
            if f"/{excl}/" in tarinfo.name or tarinfo.name.endswith(f"/{excl}"):
                return None
        if tarinfo.name.endswith("/install.lock"):
            return None
        return tarinfo

    with tarfile.open(out_tar, "w:gz") as tar:
        tar.add(docs_dir, arcname="documents", filter=_filter)
    size_mb = out_tar.stat().st_size / 1024 / 1024
    log(f"documents archives : {out_tar.name} ({size_mb:.1f} Mo)")


# ---------------------------------------------------------------------------
# Etape 4 - Metadonnees de version
# ---------------------------------------------------------------------------

def write_metadata(out_dir: Path, pack: str, version: str,
                   changelog: str, doli_version: str) -> Path:
    """Ecrit un pack.json decrivant la version (lu ensuite par le bot)."""
    import json
    meta_path = out_dir / "pack.json"
    meta = {
        "pack": pack,
        "version": version,
        "dolibarr_image": f"jgh/dolibarr:{doli_version}",
        "dolibarr_min_version": doli_version,
        "compose_service": "dolib",
        "changelog": changelog,
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "artifacts": {
            "sql": f"pack_{pack}_{version}.sql",
            "custom": f"custom_{pack}_{version}.tar.gz",
            "documents": f"documents_{pack}_{version}.tar.gz",
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    log(f"Metadonnees ecrites : {meta_path.name}")
    return meta_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Publication d'un pack Dolibarr JGH")
    parser.add_argument("--pack", required=True, choices=PACK_REPOS.keys())
    parser.add_argument("--version", required=True, help="semver, ex: 1.0.0")
    parser.add_argument("--doli-root", required=True,
                        help="racine Dolibarr (contient conf/, custom/)")
    parser.add_argument("--doli-version", default="22.0.4",
                        help="version Dolibarr (determine l'image)")
    parser.add_argument("--changelog", default="", help="notes de version")
    parser.add_argument("--out-dir", default=None,
                        help="dossier de sortie des artefacts (defaut: temp)")
    parser.add_argument("--dry-run", action="store_true",
                        help="calcule tout sans produire ni pousser")
    parser.add_argument("--no-push", action="store_true",
                        help="produit les artefacts sans pousser vers GitHub")
    args = parser.parse_args()

    log(f"=== Publication pack '{args.pack}' v{args.version} "
        f"(Dolibarr {args.doli_version}) ===")
    if args.dry_run:
        log(">>> MODE DRY-RUN : aucune ecriture, aucun push <<<")

    conf = read_dolibarr_conf(args.doli_root)
    log(f"Base : {conf['db_name']} @ {conf['db_host']} | "
        f"documents : {conf['data_root']}")

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix=f"jgh-pack-{args.pack}-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Dossier de sortie : {out_dir}")

    sql_file = out_dir / f"pack_{args.pack}_{args.version}.sql"
    custom_tar = out_dir / f"custom_{args.pack}_{args.version}.tar.gz"
    docs_tar = out_dir / f"documents_{args.pack}_{args.version}.tar.gz"

    dump_database(conf, sql_file, args.dry_run)
    clean_sql_references(sql_file, args.dry_run)
    archive_custom(args.doli_root, custom_tar, args.dry_run)
    archive_documents(conf, docs_tar, args.dry_run)
    if not args.dry_run:
        write_metadata(out_dir, args.pack, args.version, args.changelog,
                       args.doli_version)

    if args.dry_run or args.no_push:
        log("Push GitHub ignore (dry-run ou --no-push).")
        log(f"Artefacts prets dans : {out_dir}")
    else:
        log(f"Push vers {PACK_REPOS[args.pack]} - a activer quand le repo "
            "et le token seront prets.")

    log("=== Termine ===")
    if not args.dry_run:
        log(f"SQL      : {sql_file}")
        log(f"custom   : {custom_tar}")
        log(f"documents: {docs_tar}")


if __name__ == "__main__":
    main()
