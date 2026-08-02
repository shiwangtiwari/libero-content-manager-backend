-- ============================================================
-- Libero Content Manager — Autonomous Edition
-- Supabase Schema + RLS Policies
-- Run this ENTIRE file in Supabase SQL Editor (one block)
-- ============================================================


-- ── 1. posts ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS posts (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW(),
  content           TEXT NOT NULL,
  platform          TEXT DEFAULT 'linkedin'
                    CHECK (platform IN ('linkedin', 'medium')),
  status            TEXT DEFAULT 'draft'
                    CHECK (status IN (
                      'draft', 'approved', 'scheduled',
                      'posted', 'rejected', 'failed',
                      'pending_reschedule', 'expired'
                    )),
  scheduled_time    TEXT,                    -- plain IST string e.g. "2025-08-05 08:30"
  posted_time       TIMESTAMPTZ,
  linkedin_post_id  TEXT,
  image_url         TEXT,
  image_generator   TEXT CHECK (image_generator IN ('chatgpt', 'gemini', 'none')),
  viral_score       INTEGER CHECK (viral_score BETWEEN 0 AND 100),
  signal_card       JSONB DEFAULT '{}'::jsonb,
  edit_history      JSONB DEFAULT '[]'::jsonb,
  telegram_message_id TEXT,
  reschedule_count  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_scheduled_time ON posts(scheduled_time);


-- ── 2. content_signals ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS content_signals (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  source        TEXT CHECK (source IN (
                  'linkedin_trending', 'past_post_gap', 'telegram_input'
                )),
  topic         TEXT NOT NULL,
  raw_data      JSONB DEFAULT '{}'::jsonb,
  used          BOOLEAN DEFAULT FALSE,
  used_in_post  UUID REFERENCES posts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_used ON content_signals(used);
CREATE INDEX IF NOT EXISTS idx_signals_source ON content_signals(source);


-- ── 3. telegram_inputs ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS telegram_inputs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  message       TEXT NOT NULL,
  source        TEXT DEFAULT 'telegram'
                CHECK (source IN ('telegram', 'dashboard')),
  used          BOOLEAN DEFAULT FALSE,
  used_in_post  UUID REFERENCES posts(id) ON DELETE SET NULL
);


-- ── 4. session_health ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session_health (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  platform        TEXT NOT NULL CHECK (platform IN ('claude', 'chatgpt', 'gemini')),
  last_checked    TIMESTAMPTZ DEFAULT NOW(),
  last_success    TIMESTAMPTZ,
  is_healthy      BOOLEAN DEFAULT TRUE,
  failure_count   INTEGER DEFAULT 0,
  last_error      TEXT,
  UNIQUE(platform)
);

-- Seed initial rows (safe to run multiple times due to ON CONFLICT)
INSERT INTO session_health (platform)
VALUES ('claude'), ('chatgpt'), ('gemini')
ON CONFLICT (platform) DO NOTHING;


-- ── 5. posted_metrics ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS posted_metrics (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id       UUID REFERENCES posts(id) ON DELETE CASCADE,
  fetched_at    TIMESTAMPTZ DEFAULT NOW(),
  impressions   INTEGER DEFAULT 0,
  likes         INTEGER DEFAULT 0,
  comments      INTEGER DEFAULT 0,
  shares        INTEGER DEFAULT 0,
  clicks        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metrics_post_id ON posted_metrics(post_id);


-- ── 6. updated_at trigger ────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS posts_updated_at ON posts;
CREATE TRIGGER posts_updated_at
  BEFORE UPDATE ON posts
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ── 7. RLS — Enable on all tables ────────────────────────────────────────────
-- Backend uses service role key → bypasses RLS entirely.
-- RLS gates only the anon key used by Vercel frontend.

ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE posted_metrics ENABLE ROW LEVEL SECURITY;

-- posts: frontend can read all, update content and status only
DROP POLICY IF EXISTS "posts_select" ON posts;
CREATE POLICY "posts_select" ON posts FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS "posts_update" ON posts;
CREATE POLICY "posts_update" ON posts FOR UPDATE TO anon
  USING (true) WITH CHECK (true);

-- content_signals: frontend read only
DROP POLICY IF EXISTS "signals_select" ON content_signals;
CREATE POLICY "signals_select" ON content_signals FOR SELECT TO anon USING (true);

-- telegram_inputs: frontend can read and insert (Input view submissions)
DROP POLICY IF EXISTS "inputs_select" ON telegram_inputs;
CREATE POLICY "inputs_select" ON telegram_inputs FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS "inputs_insert" ON telegram_inputs;
CREATE POLICY "inputs_insert" ON telegram_inputs FOR INSERT TO anon WITH CHECK (true);

-- session_health: frontend read only
DROP POLICY IF EXISTS "health_select" ON session_health;
CREATE POLICY "health_select" ON session_health FOR SELECT TO anon USING (true);

-- posted_metrics: frontend read only
DROP POLICY IF EXISTS "metrics_select" ON posted_metrics;
CREATE POLICY "metrics_select" ON posted_metrics FOR SELECT TO anon USING (true);


-- ── Done ──────────────────────────────────────────────────────────────────────
-- All 5 tables created, indexes added, RLS enabled, seed rows inserted.
-- Next: set Railway env vars and deploy.
