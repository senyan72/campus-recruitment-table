from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260803_schema_compat.sql"


def test_schema_compat_migration_covers_current_payload_gaps():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "add column if not exists group_name" in sql
    assert "add column if not exists salary_range" in sql
    assert "add column if not exists headcount" in sql
    assert "create table if not exists public.app_users" in sql
    assert "alter table public.app_users enable row level security" in sql
    assert "create or replace function public.viewer_login" in sql
    assert "create or replace function public.viewer_has_accounts" in sql
    assert "grant execute on function public.viewer_login" in sql
