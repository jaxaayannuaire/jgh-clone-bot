"""
deploy_test.py — Voie B : déploiement d'une app Dolibarr via le CoolifyConnector.

Teste la chaîne COMPLÈTE par API (pas l'UI) :
  create_compose_application → deploy → suivi → (option) suppression.

Le repo, la branche et l'UUID de deploy key sont lus depuis le .env
(section GitHub/déploiement ci-dessous) pour ne rien coder en dur.

Usage :
    venv/bin/python deploy_test.py           # crée + déploie, puis propose de suivre
    venv/bin/python deploy_test.py --cleanup # + propose la suppression à la fin
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

from coolify_connector import (
    CoolifyConnector, CoolifyConfig,
    CoolifyError, CoolifyAuthError, CoolifyDomainConflict, CoolifyNotFound,
)

load_dotenv()


def ok(m): print(f"  \033[32m✓\033[0m {m}")
def ko(m): print(f"  \033[31m✗\033[0m {m}")
def info(m): print(f"  \033[36mi\033[0m {m}")


def build_connector() -> CoolifyConnector:
    cfg = CoolifyConfig(
        base_url=os.environ["COOLIFY_BASE_URL"],
        token=os.environ["COOLIFY_TOKEN"],
        server_uuid=os.environ["COOLIFY_SERVER_UUID"],
        project_uuid=os.environ["COOLIFY_PROJECT_UUID"],
        environment_name=os.environ["COOLIFY_ENVIRONMENT_NAME"],
        environment_uuid=os.environ["COOLIFY_ENVIRONMENT_UUID"],
        timeout=int(os.environ.get("COOLIFY_TIMEOUT", "30")),
    )
    return CoolifyConnector(cfg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true",
                        help="proposer la suppression de l'app à la fin")
    args = parser.parse_args()

    # --- Paramètres de déploiement (depuis .env) ---
    repo = os.environ.get("TEST_GIT_REPOSITORY",
                          "git@github.com:jaxaayannuaire/jgh-compose-test.git")
    branch = os.environ.get("TEST_GIT_BRANCH", "main")
    key_uuid = os.environ.get("TEST_DEPLOY_KEY_UUID", "")

    if not key_uuid:
        ko("TEST_DEPLOY_KEY_UUID manquant dans le .env.")
        info("Ajouter au .env : TEST_DEPLOY_KEY_UUID=<uuid de la deploy key Coolify>")
        sys.exit(1)

    name = f"jgh-voieb-{int(time.time())}"
    domain = f"{name}.51.255.204.248.sslip.io"

    print("\033[1m=== Voie B : déploiement via le connecteur ===\033[0m")
    info(f"repo     : {repo}")
    info(f"branche  : {branch}")
    info(f"deploykey: {key_uuid}")
    info(f"app      : {name}")
    info(f"domaine  : {domain}")
    print()

    conn = build_connector()

    # --- 1. Création de l'application ---
    print("\033[1m1. Création de l'application (create_compose_application)\033[0m")
    try:
        resp = conn.create_compose_application(
            name=name,
            git_repository=repo,
            git_branch=branch,
            private_key_uuid=key_uuid,
            domain=domain,
            compose_service_name="dolib",
            instant_deploy=False,   # on déploie explicitement à l'étape 2
            force_domain_override=False,
        )
    except CoolifyDomainConflict as e:
        ko(f"Conflit de domaine : {e}")
        sys.exit(1)
    except CoolifyNotFound as e:
        ko(f"Ressource introuvable : {e}")
        info("Si ça parle de Private Key : l'UUID de deploy key est faux.")
        sys.exit(1)
    except CoolifyAuthError as e:
        ko(f"Auth : {e}")
        sys.exit(1)
    except CoolifyError as e:
        ko(f"Création : {e}")
        sys.exit(1)

    app_uuid = resp.get("uuid")
    if not app_uuid:
        ko(f"Pas d'UUID d'app dans la réponse. Brut : {str(resp)[:400]}")
        sys.exit(1)
    ok(f"App créée : uuid={app_uuid}")

    # --- 2. Déploiement ---
    print("\n\033[1m2. Déploiement (deploy)\033[0m")
    try:
        dep = conn.deploy(app_uuid)
        ok(f"Déploiement déclenché : {str(dep)[:300]}")
    except CoolifyError as e:
        ko(f"Déploiement : {e}")
        info(f"L'app existe (uuid={app_uuid}) mais le déploiement a échoué.")
        _maybe_cleanup(conn, app_uuid, args.cleanup)
        sys.exit(1)

    # --- 3. Suivi léger ---
    print("\n\033[1m3. Suivi (le déploiement tourne en arrière-plan côté Coolify)\033[0m")
    info("Le premier déploiement télécharge les images : patienter 2-6 min.")
    info(f"Vérifier dans l'UI Coolify, ou ouvrir : https://{domain}/")
    for i in range(3):
        time.sleep(10)
        try:
            app = conn.get_application(app_uuid)
            status = app.get("status", "inconnu")
            info(f"  [{(i+1)*10}s] statut = {status}")
        except CoolifyError as e:
            info(f"  lecture statut : {e}")

    print("\n\033[1m=== Résumé ===\033[0m")
    ok(f"App déployée par API : {name} (uuid={app_uuid})")
    info(f"Ouvre https://{domain}/ dans quelques minutes pour voir Dolibarr.")

    _maybe_cleanup(conn, app_uuid, args.cleanup)


def _maybe_cleanup(conn, app_uuid, cleanup_flag):
    if not cleanup_flag:
        info(f"App conservée : {app_uuid} "
             f"(relancer avec --cleanup pour proposer la suppression).")
        return
    ans = input(f"\n  Supprimer l'app {app_uuid} (avec volumes) ? [o/N] ").strip().lower()
    if ans in ("o", "oui", "y", "yes"):
        try:
            conn.delete(app_uuid, delete_volumes=True)
            ok("App supprimée.")
        except CoolifyError as e:
            ko(f"Suppression : {e} (à nettoyer dans l'UI).")
    else:
        info(f"App conservée : {app_uuid}")


if __name__ == "__main__":
    main()
