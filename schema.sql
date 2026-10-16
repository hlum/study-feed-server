PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Full card JSON lives in `data`; hot fields are copied out for querying.
CREATE TABLE IF NOT EXISTS cards (
  id          INTEGER PRIMARY KEY,
  card_type   TEXT NOT NULL CHECK (card_type IN ('poster', 'post', 'terminal', 'article')),
  topic       TEXT NOT NULL CHECK (topic IN ('networking', 'security', 'ai', 'swe', 'ios')),
  series_id   TEXT,
  part        INTEGER,
  rare        INTEGER NOT NULL DEFAULT 0,
  data        TEXT NOT NULL CHECK (json_valid(data)),
  deeper      TEXT,
  source_id   INTEGER REFERENCES sources(id),
  seen_at     TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS cards_unseen ON cards (seen_at, created_at);

-- Feed items already turned into cards (dedupe by URL hash).
CREATE TABLE IF NOT EXISTS sources (
  id        INTEGER PRIMARY KEY,
  url_hash  TEXT NOT NULL UNIQUE,
  url       TEXT NOT NULL,
  feed      TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Reader view of a card's article (GET /cards/{id}/reader); only successful extractions.
CREATE TABLE IF NOT EXISTS readers (
  card_id     INTEGER PRIMARY KEY REFERENCES cards(id),
  title       TEXT,
  url         TEXT NOT NULL,
  text        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Curriculum progress: next roadmap item per topic.
CREATE TABLE IF NOT EXISTS topics (
  topic      TEXT PRIMARY KEY,
  next_item  INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO topics (topic) VALUES ('networking'), ('security'), ('ai'), ('swe'), ('ios');

-- First quiz answer per card (later answers give no XP).
CREATE TABLE IF NOT EXISTS answers (
  card_id     INTEGER PRIMARY KEY REFERENCES cards(id),
  choice      INTEGER NOT NULL,
  correct     INTEGER NOT NULL,
  xp          INTEGER NOT NULL,
  answered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- SM-2 state.
CREATE TABLE IF NOT EXISTS reviews (
  card_id     INTEGER PRIMARY KEY REFERENCES cards(id),
  ease        REAL NOT NULL DEFAULT 2.5,
  interval_d  REAL NOT NULL DEFAULT 0,
  reps        INTEGER NOT NULL DEFAULT 0,
  due_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_due ON reviews (due_at);

CREATE TABLE IF NOT EXISTS reactions (
  card_id  INTEGER NOT NULL REFERENCES cards(id),
  kind     TEXT NOT NULL CHECK (kind IN ('got_it', 'save')),
  at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (card_id, kind)
);

-- Ask Claude threads.
CREATE TABLE IF NOT EXISTS messages (
  id       INTEGER PRIMARY KEY,
  card_id  INTEGER NOT NULL REFERENCES cards(id),
  role     TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  text     TEXT NOT NULL,
  at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- One row per calendar day with activity; drives streak and freezes.
CREATE TABLE IF NOT EXISTS days (
  day          TEXT PRIMARY KEY,           -- YYYY-MM-DD
  cards_seen   INTEGER NOT NULL DEFAULT 0,
  quizzes      INTEGER NOT NULL DEFAULT 0,
  freeze_used  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS badges (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  rule       TEXT NOT NULL,               -- human-readable; checked in code
  earned_at  TEXT
);
INSERT OR IGNORE INTO badges (id, name, rule) VALUES
  ('streak-7',        '7-day streak',    'streak reaches 7'),
  ('streak-30',       '30-day streak',   'streak reaches 30'),
  ('first-rare-card', 'First rare card', 'answer a rare card correctly'),
  ('read-an-rfc',     'Read an RFC',     'open deeper on a card whose source is an RFC'),
  ('cards-100',       'First 100 cards', '100 cards seen'),
  ('perfect-review',  'Clean sweep',     'clear the review queue with no Again');

CREATE TABLE IF NOT EXISTS meta (
  key    TEXT PRIMARY KEY,
  value  TEXT
);
