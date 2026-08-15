"""
test_coolify.py — Validation du CoolifyConnector contre l'instance RÉELLE.

But : découvrir les écarts spec/réalité de Coolify 4.1.2 AVANT de brancher
le connecteur au bot. On procède du plus sûr au plus engageant.

Usage :
    pip install requests python-dotenv
    cp .env.example .env    # remplir COOLIFY_TOKEN au minimum
    python test_coolify.py              # lectures seules (sûr)
    python test_coolify.py --deploy     # + création/suppression d'une app test

Aucune app n'est créée sans le flag --deploy. Toute app créée par ce script
est nommée 'jgh-selftest-*' et proposée à la suppression en fin de test.
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

from coolify_connector import (
    CoolifyConnector,
    CoolifyConfig,
    CoolifyError,
    CoolifyAuthError,
    CoolifyDomainConflict,
)

load_dotenv()


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


def ok(msg): print(f"  \033[32m✓\033[0m {msg}")
def ko(msg): print(f"  \033[31m✗\033[0m {msg}")
def info(msg): print(f"  \033[36mi\033[0m {msg}")
def section(msg): print(f"\n\033[1m{msg}\033[0m")


# ---------------------------------------------------------------------------
# Étape 1 — Lectures (aucun effet de bord)
# ---------------------------------------------------------------------------

def test_reads(conn: CoolifyConnector) -> bool:
    section("1. LECTURES (sûr, aucun effet de bord)")
    all_ok = True

    # 1a. healthcheck
    try:
        h = conn.healthcheck()
        if h["api_reachable"] and h["server_found"]:
            ok(f"API joignable, serveur trouvé (reachable={h['server_reachable']}, "
               f"usable={h['server_usable']})")
        else:
            ko(f"Serveur cible introuvable dans la liste : {h}")
            all_ok = False
        if not h["wildcard_domain"]:
            info("wildcard_domain non configuré — normal en test, "
                 "à poser avant les vrais sous-domaines yessalerp.com")
    except CoolifyAuthError as e:
        ko(f"Auth : {e}")
        return False   # inutile de continuer si le jeton est mauvais
    except CoolifyError as e:
        ko(f"healthcheck : {e}")
        all_ok = False

    # 1b. liste des serveurs
    try:
        servers = conn.list_servers()
        ok(f"{len(servers)} serveur(s) listé(s)")
        for s in servers:
            info(f"  - {s.get('name')} ({s.get('uuid')}) "
                 f"ip={s.get('ip')} usable={s.get('is_usable')}")
    except CoolifyError as e:
        ko(f"list_servers : {e}")
        all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Étape 2 — Création + suppression d'une app test (flag --deploy requis)
# ---------------------------------------------------------------------------

def test_deploy_cycle(conn: CoolifyConnector) -> bool:
    section("2. CYCLE CRÉATION → SUPPRESSION (engageant, --deploy)")
    info("Ce test valide que le connecteur SAIT parler à l'API de création.")
    info("Il n'utilise pas encore un vrai pack : le but est de vérifier les")
    info("payloads et les réponses, pas de déployer Dolibarr.")

    # NB : sans repo de pack réel + deploy key, la création peut échouer côté
    # Coolify (repo injoignable). C'est ATTENDU et INSTRUCTIF : on veut voir
    # le message d'erreur exact que renvoie l'API 4.1.2, pour caler le connecteur.
    app_name = f"jgh-selftest-{int(time.time())}"
    test_domain = f"{app_name}.sslip.io"   # domaine auto, pas de DNS requis

    info(f"Tentative de création : name={app_name}, domain={test_domain}")
    info("private_key_uuid='PLACEHOLDER' et repo factice : on observe la réponse.")

    try:
        resp = conn.create_compose_application(
            name=app_name,
            git_repository="git@github.com:jaxaayannuaire/jgh-pack-pos.git",
            git_branch="main",
            private_key_uuid="PLACEHOLDER-DEPLOY-KEY-UUID",
            domain=test_domain,
            instant_deploy=False,
            force_domain_override=False,
        )
        app_uuid = resp.get("uuid")
        if app_uuid:
            ok(f"App créée : uuid={app_uuid}")
            info(f"Réponse brute (extrait) : {str(resp)[:400]}")
            _cleanup(conn, app_uuid)
        else:
            ko(f"Création : pas d'uuid dans la réponse. Brut : {str(resp)[:400]}")
            return False

    except CoolifyDomainConflict as e:
        info(f"409 conflit de domaine correctement détecté : {e}")
        ok("La gestion du 409 fonctionne (le domaine existait déjà).")
    except CoolifyAuthError as e:
        ko(f"Auth insuffisante pour créer : {e} "
           f"(le jeton a-t-il la permission write ?)")
        return False
    except CoolifyError as e:
        # On distingue : erreur de payload (à corriger) vs repo/clé invalide (attendu)
        info(f"Erreur de création : {e}")
        info("Si le message parle du repo/clé/deploy key → ATTENDU (placeholder).")
        info("Si le message parle d'un champ manquant/invalide → à corriger dans le payload.")
        ko("Voir le message ci-dessus pour trancher.")
        return False

    return True


def _cleanup(conn: CoolifyConnector, app_uuid: str) -> None:
    """Supprime l'app de test créée, pour ne rien laisser traîner."""
    ans = input(f"\n  Supprimer l'app test {app_uuid} ? [O/n] ").strip().lower()
    if ans in ("", "o", "oui", "y", "yes"):
        try:
            conn.delete(app_uuid, delete_volumes=True)
            ok(f"App test {app_uuid} supprimée.")
        except CoolifyError as e:
            ko(f"Échec suppression (à nettoyer à la main) : {e}")
    else:
        info(f"App test conservée : {app_uuid} (à supprimer manuellement).")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test du CoolifyConnector")
    parser.add_argument("--deploy", action="store_true",
                        help="active le test de création/suppression d'app")
    args = parser.parse_args()

    print("\033[1m=== Test CoolifyConnector contre l'instance réelle ===\033[0m")

    try:
        conn = build_connector()
    except KeyError as e:
        print(f"\033[31mVariable d'environnement manquante : {e}\033[0m")
        print("Remplir le .env (au minimum COOLIFY_TOKEN).")
        sys.exit(1)

    reads_ok = test_reads(conn)

    if not reads_ok:
        print("\n\033[31mLes lectures ont échoué — corriger avant d'aller plus loin.\033[0m")
        sys.exit(1)

    if args.deploy:
        test_deploy_cycle(conn)
    else:
        section("2. CYCLE DE DÉPLOIEMENT")
        info("Ignoré (relancer avec --deploy pour tester création/suppression).")

    print("\n\033[1m=== Fin ===\033[0m")


if __name__ == "__main__":
    main()
