"""
duckdb_client.py — Connexion DuckDB unique et stable (pattern JGH Alert Bot).

DuckDB n'aime pas les connexions concurrentes multi-process sur un même fichier.
On garde UNE connexion, ouverte au démarrage, réutilisée partout. Les écritures
du bot sont séquentielles (un job à la fois), donc pas de contention.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import duckdb

logger = logging.getLogger("jgh_clone.db")


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Connexion unique, réutilisée pour toute la vie du process.
        self._con = duckdb.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        schema_file = Path(__file__).with_name("schema.sql")
        self._con.execute(schema_file.read_text(encoding="utf-8"))
        logger.info("Schéma DuckDB initialisé (%s)", self.db_path)

    # -- Helpers génériques -------------------------------------------------

    def execute(self, sql: str, params: Optional[list] = None):
        return self._con.execute(sql, params or [])

    def fetchone(self, sql: str, params: Optional[list] = None) -> Optional[tuple]:
        return self._con.execute(sql, params or []).fetchone()

    def fetchall(self, sql: str, params: Optional[list] = None) -> list[tuple]:
        return self._con.execute(sql, params or []).fetchall()

    # -- clone_jobs ---------------------------------------------------------

    def create_job(self, client_name: str, subdomain: str,
                   git_repository: str, git_branch: str,
                   idempotency_key: str) -> int:
        job_id = self._con.execute(
            "SELECT nextval('seq_clone_jobs')"
        ).fetchone()[0]
        self._con.execute(
            """INSERT INTO clone_jobs
               (id, job_type, idempotency_key, client_name, subdomain,
                git_repository, git_branch, status, dry_run)
               VALUES (?, 'provision', ?, ?, ?, ?, ?, 'pending', TRUE)""",
            [job_id, idempotency_key, client_name, subdomain,
             git_repository, git_branch],
        )
        return job_id

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self.fetchone(
            """SELECT id, job_type, client_name, subdomain, git_repository,
                      git_branch, coolify_app_uuid, status, error_message,
                      stdout_log, created_at, resolved_at
               FROM clone_jobs WHERE id = ?""", [job_id])
        if not row:
            return None
        keys = ["id", "job_type", "client_name", "subdomain", "git_repository",
                "git_branch", "coolify_app_uuid", "status", "error_message",
                "stdout_log", "created_at", "resolved_at"]
        return dict(zip(keys, row))

    def recent_jobs(self, limit: int = 10) -> list[dict]:
        rows = self.fetchall(
            """SELECT id, client_name, subdomain, status, created_at
               FROM clone_jobs ORDER BY id DESC LIMIT ?""", [limit])
        keys = ["id", "client_name", "subdomain", "status", "created_at"]
        return [dict(zip(keys, r)) for r in rows]

    def job_exists_for_key(self, idempotency_key: str) -> Optional[int]:
        row = self.fetchone(
            "SELECT id FROM clone_jobs WHERE idempotency_key = ?",
            [idempotency_key])
        return row[0] if row else None

    def set_job_status(self, job_id: int, status: str,
                       app_uuid: Optional[str] = None,
                       error: Optional[str] = None,
                       append_log: Optional[str] = None,
                       resolved: bool = False) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        if app_uuid is not None:
            sets.append("coolify_app_uuid = ?"); params.append(app_uuid)
        if error is not None:
            sets.append("error_message = ?"); params.append(error)
        if append_log is not None:
            sets.append("stdout_log = COALESCE(stdout_log,'') || ?")
            params.append(append_log + "\n")
        if resolved:
            sets.append("resolved_at = current_timestamp")
        if status == "confirmed":
            sets.append("confirmed_at = current_timestamp")
            sets.append("dry_run = FALSE")
        params.append(job_id)
        self._con.execute(
            f"UPDATE clone_jobs SET {', '.join(sets)} WHERE id = ?", params)

    # -- pending_actions ----------------------------------------------------

    def create_pending(self, job_id: int, action_type: str, summary: str) -> int:
        pid = self._con.execute(
            "SELECT nextval('seq_pending_actions')").fetchone()[0]
        self._con.execute(
            """INSERT INTO pending_actions
               (id, job_id, action_type, summary, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            [pid, job_id, action_type, summary])
        return pid

    def get_pending(self, pending_id: int) -> Optional[dict]:
        row = self.fetchone(
            """SELECT id, job_id, action_type, summary, status
               FROM pending_actions WHERE id = ?""", [pending_id])
        if not row:
            return None
        keys = ["id", "job_id", "action_type", "summary", "status"]
        return dict(zip(keys, row))

    def resolve_pending(self, pending_id: int, status: str) -> None:
        self._con.execute(
            """UPDATE pending_actions
               SET status = ?, resolved_at = current_timestamp
               WHERE id = ?""", [status, pending_id])

    def close(self) -> None:
        self._con.close()
