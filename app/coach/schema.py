"""AI 陪伴 SQLite schema。"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS coach_users (
  id TEXT PRIMARY KEY,
  account TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
  user_id TEXT PRIMARY KEY REFERENCES coach_users(id),
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_assets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strengths (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readiness_assessments (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  confidence TEXT,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_documents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  filename TEXT,
  parse_status TEXT NOT NULL,
  text_content TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES resume_documents(id),
  user_id TEXT NOT NULL REFERENCES coach_users(id),
  parent_id TEXT,
  label TEXT,
  sections_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_suggestions (
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  scenario TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_matches (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  job_id TEXT,
  job_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storybank (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interview_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  job_json TEXT,
  status TEXT NOT NULL,
  questions_json TEXT NOT NULL,
  turns_json TEXT NOT NULL DEFAULT '[]',
  feedback_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  source TEXT,
  estimated_minutes INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
  id TEXT PRIMARY KEY,
  owner_id TEXT,
  filename TEXT NOT NULL,
  content_type TEXT,
  tags_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'ready',
  sha256 TEXT,
  byte_size INTEGER,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
  chunk_index INTEGER NOT NULL,
  heading TEXT,
  text TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks(document_id);

CREATE TABLE IF NOT EXISTS llm_call_logs (
  id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  schema_name TEXT,
  provider TEXT,
  model TEXT,
  ok INTEGER NOT NULL,
  fallback INTEGER NOT NULL,
  latency_ms INTEGER,
  error TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  created_at TEXT NOT NULL
);
"""
