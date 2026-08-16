-- Schéma DuckDB — JGH Clone Bot.

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
    instance_type VARCHAR DEFAULT 'client',         -- client|test (gouverne la suppression)
    status VARCHAR DEFAULT 'pending',               -- pending|confirmed|running|active|failed|deleted
    dry_run BOOLEAN DEFAULT TRUE,
    stdout_log TEXT,                                -- trace des étapes
    error_message TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp,
    confirmed_at TIMESTAMP,
    online_at TIMESTAMP,                            -- passage en 'active' (mise en ligne)
    resolved_at TIMESTAMP,
    deleted_at TIMESTAMP                            -- suppression effective
);

-- Migration douce : ajouter les colonnes si la table préexiste sans elles.
-- DuckDB supporte ADD COLUMN IF NOT EXISTS.
ALTER TABLE clone_jobs ADD COLUMN IF NOT EXISTS instance_type VARCHAR DEFAULT 'client';
ALTER TABLE clone_jobs ADD COLUMN IF NOT EXISTS online_at TIMESTAMP;
ALTER TABLE clone_jobs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;

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
