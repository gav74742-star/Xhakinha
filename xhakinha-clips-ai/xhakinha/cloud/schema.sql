-- Esquema ÚNICO do coordenador (usado pelo Python/SQLite e pelo Worker JS/D1).
CREATE TABLE IF NOT EXISTS executors(
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, caps TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  registered REAL, last_seen REAL, session_deadline REAL, meta TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT UNIQUE, payload TEXT NOT NULL DEFAULT '{}',
  priority INTEGER NOT NULL DEFAULT 5, status TEXT NOT NULL DEFAULT 'queued',
  needs TEXT NOT NULL DEFAULT '[]', prefer TEXT NOT NULL DEFAULT '[]', est_seconds REAL NOT NULL DEFAULT 60,
  owner TEXT, fence INTEGER NOT NULL DEFAULT 0, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 6, run_after REAL NOT NULL DEFAULT 0,
  checkpoint TEXT, result TEXT, error TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS tasks_claim ON tasks(status, run_after, priority);
CREATE TABLE IF NOT EXISTS providers(
  name TEXT PRIMARY KEY, config TEXT NOT NULL DEFAULT '{}', breaker TEXT NOT NULL DEFAULT 'closed',
  failures INTEGER NOT NULL DEFAULT 0, opens INTEGER NOT NULL DEFAULT 0, opened_at REAL, last_error TEXT,
  paused_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS usage(
  provider TEXT NOT NULL, period TEXT NOT NULL, unit TEXT NOT NULL, used REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(provider, period, unit));
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, owner TEXT, fence INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, executor TEXT, provider TEXT, fence INTEGER,
  outcome TEXT, ts REAL, seconds REAL, note TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, component TEXT, message TEXT);
