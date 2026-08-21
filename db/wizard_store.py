"""
wizard_store.py — Persistance des sessions de wizard (DuckDB).

Une session représente un wizard en cours pour un utilisateur : son type, son
étape courante, les données collectées, et son statut. Persistée en base pour :
  - survivre au redémarrage du bot (pas de perte de session en cours) ;
  - expirer les sessions abandonnées (expires_at) ;
  - garder une trace d'audit (qui a fait quoi, quand) ;
  - empêcher la double validation (status).

Le store réutilise la connexion DuckDB unique du bot (passée au constructeur),
ce qui garantit la sérialisation des accès (pas d'écritures concurrentes).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("jgh_clone.wizard.store")

# Durée de vie d'une session inactive (minutes) avant expiration.
DEFAULT_TTL_MINUTES = 15


class WizardStore:
    """Accès aux sessions de wizard, sur la connexion DuckDB du bot."""

    def __init__(self, connection, ttl_minutes: int = DEFAULT_TTL_MINUTES):
        self._con = connection
        self._ttl = ttl_minutes
        self._init_schema()

    def _init_schema(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "wizard_schema.sql")) as f:
            self._con.execute(f.read())

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _expiry(self) -> datetime:
        return self._now() + timedelta(minutes=self._ttl)

    @staticmethod
    def _load_data(raw: Any) -> dict:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    # -- Cycle de vie d'une session ----------------------------------------

    def get_active_session(self, user_id: int) -> Optional[dict]:
        """Renvoie la session active (non expirée) d'un utilisateur, ou None.

        Marque au passage les sessions dépassées comme 'expired'.
        """
        # Expirer d'abord les sessions dépassées de cet utilisateur.
        self._con.execute(
            """UPDATE wizard_sessions
               SET status = 'expired', updated_at = current_timestamp
               WHERE user_id = ? AND status = 'active'
                 AND expires_at IS NOT NULL AND expires_at < current_timestamp""",
            [user_id])

        row = self._con.execute(
            """SELECT id, user_id, chat_id, wizard_type, current_step,
                      step_index, collected_data, status, message_id,
                      created_at, expires_at
               FROM wizard_sessions
               WHERE user_id = ? AND status = 'active'
               ORDER BY id DESC LIMIT 1""", [user_id]).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "user_id": row[1], "chat_id": row[2],
            "wizard_type": row[3], "current_step": row[4],
            "step_index": row[5], "collected_data": self._load_data(row[6]),
            "status": row[7], "message_id": row[8],
            "created_at": row[9], "expires_at": row[10],
        }

    def get_session(self, session_id: int) -> Optional[dict]:
        row = self._con.execute(
            """SELECT id, user_id, chat_id, wizard_type, current_step,
                      step_index, collected_data, status, message_id,
                      created_at, expires_at
               FROM wizard_sessions WHERE id = ?""", [session_id]).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "user_id": row[1], "chat_id": row[2],
            "wizard_type": row[3], "current_step": row[4],
            "step_index": row[5], "collected_data": self._load_data(row[6]),
            "status": row[7], "message_id": row[8],
            "created_at": row[9], "expires_at": row[10],
        }

    def create_session(self, user_id: int, chat_id: int,
                        wizard_type: str, first_step: str,
                        initial_data: Optional[dict] = None) -> int:
        """Crée une session active. Renvoie son id.

        initial_data : données pré-remplies (ex. contexte injecté par la
        commande, comme une liste d'options calculée au démarrage).
        """
        session_id = self._con.execute(
            "SELECT nextval('seq_wizard_sessions')").fetchone()[0]
        data_json = json.dumps(initial_data, default=str) if initial_data else '{}'
        self._con.execute(
            """INSERT INTO wizard_sessions
               (id, user_id, chat_id, wizard_type, current_step, step_index,
                collected_data, status, expires_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, 'active', ?)""",
            [session_id, user_id, chat_id, wizard_type, first_step,
             data_json, self._expiry()])
        return session_id

    def set_message_id(self, session_id: int, message_id: int):
        """Mémorise le message Telegram à éditer (fil unique du wizard)."""
        self._con.execute(
            "UPDATE wizard_sessions SET message_id = ? WHERE id = ?",
            [message_id, session_id])

    def save_answer(self, session_id: int, key: str, value: Any):
        """Enregistre une réponse dans collected_data et prolonge l'expiration."""
        sess = self.get_session(session_id)
        if not sess:
            return
        data = sess["collected_data"]
        data[key] = value
        self._con.execute(
            """UPDATE wizard_sessions
               SET collected_data = ?, updated_at = current_timestamp,
                   expires_at = ?
               WHERE id = ?""",
            [json.dumps(data, default=str), self._expiry(), session_id])

    def goto_step(self, session_id: int, step_key: str, step_index: int):
        """Positionne la session sur une étape donnée et prolonge l'expiration."""
        self._con.execute(
            """UPDATE wizard_sessions
               SET current_step = ?, step_index = ?,
                   updated_at = current_timestamp, expires_at = ?
               WHERE id = ?""",
            [step_key, step_index, self._expiry(), session_id])

    def complete_session(self, session_id: int):
        """Marque la session comme terminée (validée). Anti-double-validation."""
        self._con.execute(
            """UPDATE wizard_sessions
               SET status = 'completed', updated_at = current_timestamp
               WHERE id = ?""", [session_id])

    def cancel_session(self, session_id: int):
        self._con.execute(
            """UPDATE wizard_sessions
               SET status = 'cancelled', updated_at = current_timestamp
               WHERE id = ?""", [session_id])

    def expire_stale(self) -> int:
        """Marque 'expired' toutes les sessions actives dépassées (tâche
        périodique). Renvoie le nombre de sessions expirées."""
        before = self._con.execute(
            """SELECT count(*) FROM wizard_sessions
               WHERE status = 'active' AND expires_at IS NOT NULL
                 AND expires_at < current_timestamp""").fetchone()[0]
        self._con.execute(
            """UPDATE wizard_sessions
               SET status = 'expired', updated_at = current_timestamp
               WHERE status = 'active' AND expires_at IS NOT NULL
                 AND expires_at < current_timestamp""")
        return before

    def claim_completion(self, session_id: int) -> bool:
        """
        Tente de passer la session de 'active' à 'completed' de façon atomique.
        Renvoie True si CETTE tentative a effectué la transition, False si la
        session n'était pas/plus active (déjà validée, annulée, expirée).

        C'est le verrou anti-double-validation : deux clics sur VALIDER, un seul
        obtient True.
        """
        # DuckDB ne renvoie pas le nombre de lignes affectées via execute() de
        # façon portable ; on lit le statut avant/après dans la même connexion
        # (sérialisée), ce qui est sûr ici.
        sess = self.get_session(session_id)
        if not sess or sess["status"] != "active":
            return False
        self._con.execute(
            """UPDATE wizard_sessions
               SET status = 'completed', updated_at = current_timestamp
               WHERE id = ? AND status = 'active'""", [session_id])
        return True
