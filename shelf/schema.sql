-- shelf: schema for AI answer-engine visibility measurement
-- Design note: one row per (prompt, engine, repetition). We never collapse
-- repetitions at write time, because run-to-run variance IS a finding.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS brands (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    domain      TEXT,
    is_focus    INTEGER NOT NULL DEFAULT 0,  -- 1 = a brand we are auditing for
    aliases     TEXT NOT NULL DEFAULT '[]',  -- JSON array of alternate spellings
    -- Some brand names are ordinary English words (Clay, Instantly, Warmly,
    -- Unify). For those, require the capitalised form or every 'respond
    -- instantly' becomes a phantom mention.
    case_sensitive INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL UNIQUE,
    intent      TEXT NOT NULL,   -- discovery | comparison | alternatives | objection | ...
    persona     TEXT NOT NULL,   -- practitioner | economic_buyer | technical | procurement
    stage       TEXT NOT NULL,   -- problem | compare | shortlist | validate | implement
    subject     TEXT,            -- brand this prompt is *about*, if any
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    prompt_id     INTEGER NOT NULL REFERENCES prompts(id),
    engine        TEXT NOT NULL,   -- gemini | groq | openrouter | manual_chatgpt | manual_perplexity
    model         TEXT NOT NULL,
    grounded      INTEGER NOT NULL,-- 1 = live web search enabled, 0 = model memory only
    rep           INTEGER NOT NULL,-- repetition index: 0..N-1 for the same prompt
    requested_at  TEXT NOT NULL,
    latency_ms    INTEGER,
    response      TEXT,
    error         TEXT,
    finish_reason TEXT,   -- 'length' means the answer was cut off

    UNIQUE(prompt_id, engine, model, grounded, rep)
);

CREATE TABLE IF NOT EXISTS mentions (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    brand_id    INTEGER NOT NULL REFERENCES brands(id),
    mentioned   INTEGER NOT NULL DEFAULT 0,  -- appears anywhere in the answer
    recommended INTEGER NOT NULL DEFAULT 0,  -- appears as an actual recommendation
    rank_pos    INTEGER,                     -- 1-indexed order of first appearance
    -- 1 when this brand was named in the prompt itself. Echoing the question
    -- ('alternatives to X' -> 'moving off X, here are...') is not visibility,
    -- so these are excluded from headline rates by default.
    prompted    INTEGER NOT NULL DEFAULT 0,
    snippet     TEXT
);

CREATE TABLE IF NOT EXISTS citations (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    domain      TEXT,
    title       TEXT,
    position    INTEGER
);

-- Factual claims the model makes about a focus brand, queued for human verification.
CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    brand_id     INTEGER NOT NULL REFERENCES brands(id),
    claim_text   TEXT NOT NULL,
    claim_type   TEXT,    -- pricing | feature | funding | customer | integration | company_fact
    verdict      TEXT,    -- true | false | outdated | unverifiable | (null = not yet checked)
    evidence_url TEXT,
    checked_at   TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_prompt   ON runs(prompt_id);
CREATE INDEX IF NOT EXISTS idx_mentions_run  ON mentions(run_id);
CREATE INDEX IF NOT EXISTS idx_mentions_brand ON mentions(brand_id);
CREATE INDEX IF NOT EXISTS idx_citations_run ON citations(run_id);
CREATE INDEX IF NOT EXISTS idx_claims_brand  ON claims(brand_id);
