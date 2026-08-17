-- Sessions de wizard (moteur conversationnel multi-étapes).
-- Persistées en base pour survivre aux redémarrages du bot, permettre
-- l'expiration des sessions abandonnées, l'audit et l'anti-double-validation.

CREATE TABLE IF NOT EXISTS wizard_sessions (
    id INTEGER PRIMARY KEY,
    user_id BIGINT NOT NULL,           -- Telegram user id qui mène le wizard
    chat_id BIGINT NOT NULL,           -- chat où renvoyer les messages
    wizard_type VARCHAR NOT NULL,      -- 'deployer' | 'supprimer' | 'demo' | ...
    current_step VARCHAR,              -- clé de l'étape courante
    step_index INTEGER DEFAULT 0,      -- position dans la liste d'étapes
    collected_data VARCHAR DEFAULT '{}', -- réponses {clé: valeur} en JSON
    status VARCHAR DEFAULT 'active',   -- active|completed|cancelled|expired
    message_id BIGINT,                 -- message Telegram à éditer (fil unique)
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    expires_at TIMESTAMP               -- inactivité au-delà => expired
);

CREATE SEQUENCE IF NOT EXISTS seq_wizard_sessions START 1;
