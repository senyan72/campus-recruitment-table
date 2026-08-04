"""SQLite 建表语句。"""

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    company_nature TEXT,
    industry TEXT,
    hint_apply_urls TEXT DEFAULT '[]',
    career_urls TEXT DEFAULT '[]',
    wechat_name TEXT,
    verify_status TEXT NOT NULL DEFAULT 'unverified',
    source_sheets TEXT DEFAULT '[]',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_norm ON companies(name_norm);
CREATE INDEX IF NOT EXISTS idx_companies_verify ON companies(verify_status);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company_id TEXT,
    group_name TEXT,
    company TEXT NOT NULL,
    recruit_project TEXT,
    recruit_bucket TEXT,
    company_nature TEXT,
    title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    apply_url TEXT,
    deadline TEXT,
    work_location TEXT,
    industry TEXT,
    education TEXT,
    salary_range TEXT,
    headcount TEXT,
    open_at TEXT,
    graduation_batch TEXT,
    jd_text TEXT,
    job_tags TEXT DEFAULT '[]',
    raw_category TEXT,
    parse_status TEXT DEFAULT 'ok',
    confidence REAL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'active',
    cloud_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_url, title)
);
CREATE INDEX IF NOT EXISTS idx_jobs_bucket ON jobs(recruit_bucket);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS my_status (
    job_id TEXT PRIMARY KEY,
    apply_status TEXT NOT NULL DEFAULT '未投递',
    note TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

-- Viewer 本机个人选取；不参与 Supabase 同步
CREATE TABLE IF NOT EXISTS my_pick (
    job_id TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT '个人校招投递',
    added_at TEXT NOT NULL,
    PRIMARY KEY (job_id, collection),
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_my_pick_collection ON my_pick(collection);

CREATE TABLE IF NOT EXISTS review_queue (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS source_verify_queue (
    id TEXT PRIMARY KEY,
    company_id TEXT,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS aggregator_blocklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    note TEXT
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS digest_log (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collect_queue (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    company_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    jobs_published INTEGER NOT NULL DEFAULT 0,
    jobs_queued INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_collect_queue_run ON collect_queue(run_id, status);

-- Admin 管理的 Viewer 登录账号（本地镜像；云端见 Supabase app_users）
CREATE TABLE IF NOT EXISTS app_users (
    account TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    notes TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
