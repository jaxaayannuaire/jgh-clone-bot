"""
woo_connector.py — Client de l'API REST WooCommerce (lecture) pour JGH Clone Bot.

Lit les commandes du site yessalerp.com pour proposer le provisioning des
instances vendues. WooCommerce + Woo Subscriptions restent la source de vérité
commerciale ; le bot lit, propose, et déploie (le déploiement est géré par le
CoolifyConnector, pas ici).

Étape 1 : LECTURE SEULE. La validation d'une commande (passage à 'completed')
et la création de commandes terrain viendront avec une clé écriture (étape 2).

Testé contre l'API WooCommerce v3. Principes hérités du CoolifyConnector :
    - session HTTP unique + retry réseau
    - parsing défensif
    - exceptions dédiées pour que le bot décide quoi dire sur Telegram
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("jgh_clone.woo")


# ---------------------------------------------------------------------------
# Exceptions dédiées
# ---------------------------------------------------------------------------

class WooError(Exception):
    """Erreur générique du connecteur WooCommerce."""


class WooAuthError(WooError):
    """401/403 : clés API invalides ou permission manquante."""


class WooNotFound(WooError):
    """404 : commande/ressource inexistante."""


# ---------------------------------------------------------------------------
# Mapping produit WooCommerce -> clé de pack du catalogue
# ---------------------------------------------------------------------------
# IDs produits confirmés sur yessalerp.com. Étendre ici si de nouveaux packs
# sont ajoutés au catalogue commercial.

PRODUCT_TO_PACK: dict[int, str] = {
    3508: "tambali",
    3562: "pos",
    3566: "asso",
    3581: "pro",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class WooConfig:
    base_url: str            # ex: https://yessalerp.com
    consumer_key: str        # ck_...
    consumer_secret: str     # cs_...
    timeout: int = 30

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/wp-json/wc/v3"


# ---------------------------------------------------------------------------
# Structure normalisée d'une commande (ce que le bot manipule)
# ---------------------------------------------------------------------------

@dataclass
class WooOrder:
    """Vue simplifiée d'une commande, prête pour le provisioning."""
    order_id: int
    number: str
    status: str
    date_created: str
    # Client
    first_name: str
    last_name: str
    company: str
    email: str
    phone: str
    city: str
    address: str
    # Produit / pack
    product_id: Optional[int]
    product_name: str
    pack_key: Optional[str]        # résolu via PRODUCT_TO_PACK
    # Champs YessalERP (meta)
    activite: str
    sousdomaine_saisi: str
    # Divers
    total: str
    currency: str
    payment_method_title: str

    def resolved_subdomain(self) -> str:
        """
        Cascade de génération du sous-domaine :
          1. sous-domaine saisi par le client (déjà nettoyé côté WP)
          2. sinon, slug de la société (billing.company)
          3. sinon, slug du nom d'activité (_yessal_activite)
          4. sinon, 'cmd<number>' (toujours unique)
        """
        for candidate in (self.sousdomaine_saisi,
                          _slug(self.company),
                          _slug(self.activite)):
            if candidate and len(candidate) >= 2:
                return candidate[:30]
        return f"cmd{self.number}"

    def client_label(self) -> str:
        """Libellé lisible du client pour l'affichage Telegram."""
        name = f"{self.first_name} {self.last_name}".strip()
        if self.company:
            return f"{name} ({self.company})" if name else self.company
        if self.activite:
            return f"{name} — {self.activite}" if name else self.activite
        return name or f"Client #{self.order_id}"


def _slug(text: str) -> str:
    """Normalise un texte en slug de sous-domaine (lettres/chiffres, minuscules)."""
    if not text:
        return ""
    import unicodedata
    # Translittération des accents
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return "".join(c for c in text if c.isalnum())[:30]


# ---------------------------------------------------------------------------
# Connecteur
# ---------------------------------------------------------------------------

class WooConnector:
    """Client mince de l'API REST WooCommerce (lecture)."""

    def __init__(self, config: WooConfig):
        self.cfg = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        # WooCommerce : auth Basic (consumer_key:consumer_secret) sur HTTPS.
        session.auth = (self.cfg.consumer_key, self.cfg.consumer_secret)
        # User-Agent explicite : beaucoup de pare-feux WordPress (Wordfence,
        # règles anti-bot d'hébergeurs) bloquent le UA par défaut
        # "python-requests/x.y" (403). On s'identifie proprement.
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "JGH-Clone-Bot/1.0 (+https://yessalerp.com)",
        })
        return session

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None) -> Any:
        url = f"{self.cfg.api_root}/{path.lstrip('/')}"
        try:
            resp = self._session.request(
                method, url, params=params, timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            raise WooError(f"Échec réseau vers WooCommerce ({method} {path}): {exc}") from exc

        if resp.status_code in (401, 403):
            raise WooAuthError(
                f"Auth refusée ({resp.status_code}) sur {method} {path}. "
                f"Vérifier les clés API WooCommerce et leurs permissions.")
        if resp.status_code == 404:
            raise WooNotFound(f"Introuvable (404) sur {method} {path}")
        if not resp.ok:
            raise WooError(f"Erreur WooCommerce {resp.status_code} sur {method} {path}: "
                          f"{resp.text[:300]}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            logger.warning("Réponse non-JSON de WooCommerce (%s %s)", method, path)
            return {"_raw": resp.text}

    # -- Lectures -----------------------------------------------------------

    def list_orders(self, status: str = "completed",
                    per_page: int = 20) -> list[WooOrder]:
        """
        Liste les commandes d'un statut donné (défaut 'completed'), les plus
        récentes d'abord, normalisées en WooOrder.
        """
        data = self._request("GET", "orders", params={
            "status": status,
            "per_page": per_page,
            "orderby": "date",
            "order": "desc",
        })
        if not isinstance(data, list):
            return []
        return [self._parse_order(o) for o in data]

    def get_order(self, order_id: int) -> WooOrder:
        """Détail d'une commande, normalisée."""
        data = self._request("GET", f"orders/{order_id}")
        return self._parse_order(data)

    def healthcheck(self) -> dict:
        """Vérifie que l'API répond et que les clés sont valides."""
        # Un appel léger : lister 1 commande. Si auth KO -> WooAuthError.
        self._request("GET", "orders", params={"per_page": 1})
        return {"api_reachable": True}

    # -- Parsing défensif ---------------------------------------------------

    @staticmethod
    def _parse_order(o: dict) -> WooOrder:
        billing = o.get("billing", {}) or {}

        # Premier produit de la commande (un pack = une ligne en général)
        product_id = None
        product_name = ""
        line_items = o.get("line_items") or []
        if line_items:
            product_id = line_items[0].get("product_id")
            product_name = line_items[0].get("name", "")

        # Meta YessalERP
        meta = {m.get("key"): m.get("value")
                for m in (o.get("meta_data") or [])
                if isinstance(m, dict)}

        pack_key = PRODUCT_TO_PACK.get(product_id) if product_id else None

        return WooOrder(
            order_id=o.get("id"),
            number=str(o.get("number", o.get("id", ""))),
            status=o.get("status", ""),
            date_created=o.get("date_created", ""),
            first_name=billing.get("first_name", ""),
            last_name=billing.get("last_name", ""),
            company=billing.get("company", ""),
            email=billing.get("email", ""),
            phone=billing.get("phone", ""),
            city=billing.get("city", ""),
            address=billing.get("address_1", ""),
            product_id=product_id,
            product_name=product_name,
            pack_key=pack_key,
            activite=str(meta.get("_yessal_activite", "") or ""),
            sousdomaine_saisi=str(meta.get("_yessal_sousdomaine", "") or ""),
            total=str(o.get("total", "")),
            currency=o.get("currency", ""),
            payment_method_title=o.get("payment_method_title", ""),
        )
