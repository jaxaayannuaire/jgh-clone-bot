"""
coolify_connector.py — Client de l'API Coolify v1 pour JGH Clone Bot.

Voie de déploiement : hybride Compose (un docker-compose.yml par repo de pack,
référençant l'image Dolibarr commune + volumes ; Coolify clone via deploy key).

Testé contre Coolify 4.1.2. Principes hérités de JGH Alert Bot :
    - session HTTP unique + retry réseau
    - parsing défensif (tolérant aux champs amputés par removeSensitiveData)
    - jamais d'écrasement silencieux (409 sur conflit de domaine remonté explicitement)

Aucune action destructive n'est déclenchée à l'import : ce module ne fait
qu'exposer des méthodes. La confirmation (file pending_actions) est gérée
au niveau du bot, pas ici.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("jgh_clone.coolify")


# ---------------------------------------------------------------------------
# Exceptions dédiées — le bot les attrape pour décider quoi dire sur Telegram
# ---------------------------------------------------------------------------

class CoolifyError(Exception):
    """Erreur générique du connecteur Coolify."""


class CoolifyAuthError(CoolifyError):
    """401/403 : jeton invalide ou permission manquante."""


class CoolifyDomainConflict(CoolifyError):
    """409 : le sous-domaine demandé est déjà pris (force_domain_override=false)."""


class CoolifyNotFound(CoolifyError):
    """404 : ressource inexistante (UUID erroné, app supprimée...)."""


# ---------------------------------------------------------------------------
# Configuration — alimentée depuis le .env par le bot
# ---------------------------------------------------------------------------

@dataclass
class CoolifyConfig:
    base_url: str            # ex: http://51.255.204.248:8000
    token: str               # jeton API scopé read+write+deploy
    server_uuid: str         # UUID du serveur cible (VPS de déploiement)
    project_uuid: str        # UUID du projet Coolify
    environment_name: str    # nom de l'environnement (ex: "production")
    environment_uuid: str    # UUID de l'environnement (fallback selon l'API)
    timeout: int = 30        # secondes par requête

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1"


# ---------------------------------------------------------------------------
# Connecteur
# ---------------------------------------------------------------------------

class CoolifyConnector:
    """
    Client mince de l'API Coolify v1.

    Chaque méthode publique correspond à une action du pipeline de provisioning.
    Les réponses sont renvoyées en dict brut (parsées défensivement) pour que
    le bot journalise la réalité, pas une abstraction.
    """

    def __init__(self, config: CoolifyConfig):
        self.cfg = config
        self._session = self._build_session()

    # -- Session HTTP avec retry (pattern Alert Bot) ------------------------

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        # Retry uniquement sur erreurs réseau/serveur transitoires,
        # JAMAIS sur 4xx (un 409 conflit de domaine ne doit pas être rejoué).
        retry = Retry(
            total=3,
            backoff_factor=1.5,               # 0s, 1.5s, 3s...
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PATCH", "DELETE"],
            respect_retry_after_header=True,  # Coolify renvoie Retry-After sur 429
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "Authorization": f"Bearer {self.cfg.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        return session

    # -- Cœur des requêtes --------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: Optional[dict] = None) -> Any:
        url = f"{self.cfg.api_root}/{path.lstrip('/')}"
        try:
            resp = self._session.request(
                method, url, json=payload, timeout=self.cfg.timeout,
            )
        except requests.RequestException as exc:
            # Réseau mort, DNS, timeout dur : après épuisement des retries
            raise CoolifyError(f"Échec réseau vers Coolify ({method} {path}): {exc}") from exc

        self._raise_for_status(resp, method, path)

        # Corps vide légitime (ex: certains DELETE renvoient 200 sans JSON)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            # Réponse non-JSON inattendue : on remonte le texte brut pour debug
            logger.warning("Réponse non-JSON de Coolify (%s %s): %r",
                           method, path, resp.text[:500])
            return {"_raw": resp.text}

    def _raise_for_status(self, resp: requests.Response,
                          method: str, path: str) -> None:
        if resp.ok:
            return

        # Message d'erreur le plus parlant possible pour le journal du bot
        detail = self._extract_error_detail(resp)
        code = resp.status_code

        if code in (401, 403):
            raise CoolifyAuthError(
                f"Auth refusée ({code}) sur {method} {path} : {detail}. "
                f"Vérifier le jeton et ses permissions (write/deploy)."
            )
        if code == 404:
            raise CoolifyNotFound(f"Introuvable (404) sur {method} {path} : {detail}")
        if code == 409:
            raise CoolifyDomainConflict(
                f"Conflit (409) sur {method} {path} : {detail}. "
                f"Sous-domaine probablement déjà pris."
            )
        raise CoolifyError(f"Erreur Coolify {code} sur {method} {path} : {detail}")

    @staticmethod
    def _extract_error_detail(resp: requests.Response) -> str:
        """
        Coolify renvoie selon le cas {message:...}, {error:...}, ou sur un 422
        un {message:..., errors:{champ:[raisons]}}. On déplie `errors` pour
        rendre visible le champ fautif — sans lui, un 422 est indébogable.
        """
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300] or "(corps vide)"
        if isinstance(body, dict):
            parts: list[str] = []
            if body.get("message"):
                parts.append(str(body["message"]))
            # Détail par champ (validation Laravel : {champ: [raison, ...]})
            errors = body.get("errors")
            if isinstance(errors, dict):
                for field, reasons in errors.items():
                    if isinstance(reasons, (list, tuple)):
                        parts.append(f"{field}: {'; '.join(map(str, reasons))}")
                    else:
                        parts.append(f"{field}: {reasons}")
            elif errors:
                parts.append(str(errors))
            if not parts and body.get("error"):
                parts.append(str(body["error"]))
            if parts:
                return " | ".join(parts)
        return str(body)[:300]

    # -- Lectures (permission read) -----------------------------------------

    def list_servers(self) -> list[dict]:
        """Liste les serveurs. Sert au diagnostic de connexion."""
        data = self._request("GET", "servers")
        return data if isinstance(data, list) else []

    def get_application(self, app_uuid: str) -> dict:
        """
        Détail d'une application.
        NB: selon la permission du jeton, removeSensitiveData ampute certains
        champs (dockerfile, docker_compose_raw, secrets webhook). Un champ
        absent n'est donc PAS forcément un bug — parsing défensif obligatoire.
        """
        return self._request("GET", f"applications/{app_uuid}")

    def application_status(self, app_uuid: str) -> str:
        """Statut courant d'une app ('running', 'exited', 'degraded'...)."""
        app = self.get_application(app_uuid)
        # Le champ exact varie selon la version : on tente plusieurs clés.
        return (app.get("status")
                or app.get("last_online_at")
                and "running"
                or "unknown")

    # -- Création : voie hybride Compose ------------------------------------

    def create_compose_application(
        self,
        name: str,
        git_repository: str,          # ex: git@github.com:jaxaayannuaire/jgh-pack-pos.git
        git_branch: str,              # ex: "main" (le checkout du tag se gère en amont)
        private_key_uuid: str,        # deploy key Coolify du repo du pack
        domain: str,                  # ex: client1pos.yessalerp.com (sans schéma)
        compose_service_name: str = "dolib",  # nom du service dans le compose
        docker_compose_location: str = "/docker-compose.yml",
        instant_deploy: bool = False,
        force_domain_override: bool = False,
    ) -> dict:
        """
        Crée une application Coolify à partir d'un repo Git privé contenant
        un docker-compose.yml (voie hybride confirmée).

        IMPORTANT (écart 4.1.2 validé contre l'instance réelle) : en mode
        dockercompose, le champ `domains` plat est REFUSÉ (422). Il faut
        `docker_compose_domains`, un TABLEAU D'OBJETS {name, domain} où :
          - name   = nom du service dans le docker-compose.yml qui porte le domaine
          - domain = domaine complet avec schéma (https://...)
        Coolify valide que `name` existe bien comme service du compose.

        CONVENTION D'ARTEFACT : le service applicatif principal de tous les packs
        s'appelle `dolib` (compose_service_name par défaut). Tous les
        docker-compose.yml de packs DOIVENT déclarer un service de ce nom.

        force_domain_override=False → l'API renvoie 409 si le domaine est déjà
        pris, remonté en CoolifyDomainConflict (jamais d'écrasement silencieux).

        instant_deploy=False → on sépare création et déploiement pour permettre
        l'injection des variables DOLI_* AVANT le premier boot (set_envs_bulk).
        Le bot enchaîne : create → set_envs_bulk → deploy.
        """
        # docker_compose_domains attend des URLs avec schéma.
        fqdn = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

        payload = {
            "project_uuid": self.cfg.project_uuid,
            "server_uuid": self.cfg.server_uuid,
            "environment_name": self.cfg.environment_name,
            "environment_uuid": self.cfg.environment_uuid,
            "name": name,
            "git_repository": git_repository,
            "git_branch": git_branch,
            "private_key_uuid": private_key_uuid,
            "build_pack": "dockercompose",
            "docker_compose_location": docker_compose_location,
            "docker_compose_domains": [
                {"name": compose_service_name, "domain": fqdn}
            ],
            "instant_deploy": instant_deploy,
            "force_domain_override": force_domain_override,
        }
        logger.info("Création app Compose '%s' (repo=%s, service=%s, domaine=%s)",
                    name, git_repository, compose_service_name, fqdn)
        return self._request("POST", "applications/private-deploy-key", payload)

    # -- Variables d'environnement (remplace le sed sur conf.php) -----------

    def set_envs_bulk(self, app_uuid: str, envs: dict[str, str]) -> dict:
        """
        Injecte en masse les variables DOLI_* (DB, URL_ROOT, ENABLE_MODULES...).
        C'est le remplaçant direct du `sed -i` sur conf.php du pipeline legacy.

        Format attendu par l'API : liste d'objets {key, value}.
        """
        payload = {
            "data": [
                {"key": k, "value": str(v), "is_preview": False}
                for k, v in envs.items()
            ]
        }
        logger.info("Injection de %d variables d'env sur %s", len(envs), app_uuid)
        return self._request("PATCH", f"applications/{app_uuid}/envs/bulk", payload)

    # -- Cycle de vie (permission deploy) -----------------------------------

    def deploy(self, app_uuid: str) -> dict:
        """Déclenche le déploiement (dispatch d'un ApplicationDeploymentJob)."""
        logger.info("Déploiement de %s", app_uuid)
        return self._request("POST", f"applications/{app_uuid}/start")

    def stop(self, app_uuid: str) -> dict:
        logger.info("Arrêt de %s", app_uuid)
        return self._request("POST", f"applications/{app_uuid}/stop")

    def restart(self, app_uuid: str) -> dict:
        logger.info("Redémarrage de %s", app_uuid)
        return self._request("POST", f"applications/{app_uuid}/restart")

    def delete(self, app_uuid: str,
               delete_volumes: bool = True) -> dict:
        """
        Supprime une application (résiliation).
        ATTENTION : action destructive. Le bot ne l'appelle QUE après
        confirmation explicite (file pending_actions). delete_volumes=True
        emporte les données du tenant — à confirmer deux fois côté bot.
        """
        logger.warning("SUPPRESSION de %s (volumes=%s)", app_uuid, delete_volumes)
        params = "?delete_volumes=true" if delete_volumes else ""
        return self._request("DELETE", f"applications/{app_uuid}{params}")

    # -- Diagnostic de connexion --------------------------------------------

    def healthcheck(self) -> dict:
        """
        Vérifie que l'instance répond et que le jeton est valide.
        Renvoie un petit résumé exploitable par /version ou un test de démarrage.
        """
        servers = self.list_servers()
        target = next(
            (s for s in servers if s.get("uuid") == self.cfg.server_uuid),
            None,
        )
        return {
            "api_reachable": True,
            "server_found": target is not None,
            "server_reachable": bool(target and target.get("is_reachable")),
            "server_usable": bool(target and target.get("is_usable")),
            "wildcard_domain": target.get("settings", {}).get("wildcard_domain")
                if target else None,
        }
