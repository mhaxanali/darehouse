CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    enabled_cogs TEXT DEFAULT '[]',
    tod_channel INTEGER,
    tod_role INTEGER,
    is_locked BOOLEAN NOT NULL DEFAULT 0,
    is_hard_locked BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    type TEXT NOT NULL CHECK (type IN ('truth', 'dare')),
    content TEXT NOT NULL,
    added_by INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prompts_guild_type
    ON prompts (guild_id, type);

CREATE TABLE IF NOT EXISTS prompt_bans (
    guild_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    banned_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, prompt_id)
);

CREATE INDEX IF NOT EXISTS idx_prompt_bans_guild
    ON prompt_bans (guild_id);