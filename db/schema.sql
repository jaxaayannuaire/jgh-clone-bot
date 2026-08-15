-- Schéma DuckDB — JGH Clone Bot (version minimale : flux provision).
-- Les tables pack_versions / instances viendront avec les packs (plus tard).

-- Jobs de provisioning (et, plus tard, migrate/restore)
CREATE TABLE IF NOT EXISTS clone_jobs (
    id INTEGER PRIMARY KEY,
    job_type VARCHAR NOT NULL DEFAULT 'provision',  -- provision|migrate|restore
    idempotency_key VARCHAR UNIQUE,                 -- rejeu sans doublon
    client_name VARCHAR,                            -- nom logique (ex: client1pos)
    subdomain VARCHAR,                              -- domaine complet déployé
    git_repository VARCHAR,                         -- repo déployé
    git_branch VARCHAR,
    coolify_app_uuid VARCHAR,                       -- UUID de l'app créée
    status VARCHAR DEFAULT 'pending',               -- pending|confirmed|running|active|failed
    dry_run BOOLEAN DEFAULT TRUE,
    stdout_log TEXT,                                -- trace des étapes
    error_message TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp,
    confirmed_at TIMESTAMP,
    resolved_at TIMESTAMP
);

-- File de confirmation (pattern pending_dolibarr_writes d'Alert Bot)
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    job_id INTEGER,                                 -- FK logique vers clone_jobs.id
    action_type VARCHAR,                            -- 'provision' | 'delete' | ...
    summary VARCHAR,                                -- récap montré sur Telegram
    status VARCHAR DEFAULT 'pending',               -- pending|confirmed|rejected|expired
    created_at TIMESTAMP DEFAULT current_timestamp,
    resolved_at TIMESTAMP
);

-- Séquences pour les IDs (DuckDB n'a pas d'AUTOINCREMENT implicite)
CREATE SEQUENCE IF NOT EXISTS seq_clone_jobs START 1;
CREATE SEQUENCE IF NOT EXISTS seq_pending_actions START 1;
