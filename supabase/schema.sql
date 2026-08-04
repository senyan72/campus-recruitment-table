-- 校招投递表 Supabase schema + RLS
-- 在 Supabase SQL Editor 中执行本脚本

create extension if not exists "pgcrypto";

create table if not exists public.jobs (
  id uuid primary key default gen_random_uuid(),
  company_id text,
  group_name text,
  company text not null,
  recruit_project text,
  recruit_bucket text check (recruit_bucket is null or recruit_bucket in ('校招', '应届生实习', '日常实习')),
  company_nature text,
  title text not null,
  source_url text not null,
  apply_url text,
  deadline text,
  work_location text,
  industry text,
  education text,
  salary_range text,
  headcount text,
  open_at text,
  graduation_batch text,
  jd_text text,
  job_tags jsonb default '[]'::jsonb,
  raw_category text,
  parse_status text default 'ok',
  confidence double precision default 0,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists jobs_source_title_uidx on public.jobs (source_url, title);
create index if not exists jobs_updated_idx on public.jobs (updated_at desc);
create index if not exists jobs_bucket_idx on public.jobs (recruit_bucket);
create index if not exists jobs_status_idx on public.jobs (status);

create table if not exists public.companies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  name_norm text not null unique,
  company_nature text,
  industry text,
  career_urls jsonb default '[]'::jsonb,
  wechat_name text,
  verify_status text not null default 'unverified',
  updated_at timestamptz not null default now()
);

create table if not exists public.app_meta (
  key text primary key,
  value text not null
);

insert into public.app_meta(key, value)
values ('min_version', '0.2.0')
on conflict (key) do update set value = excluded.value;

-- 更新时间触发器
create or replace function public.set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists jobs_set_updated_at on public.jobs;
create trigger jobs_set_updated_at
before update on public.jobs
for each row execute function public.set_updated_at();

-- RLS：Viewer 不直接读取 jobs/companies，岗位同步统一走带会话校验的 RPC。
-- 写操作仅 service_role（绕过 RLS）。
alter table public.jobs enable row level security;
alter table public.companies enable row level security;
alter table public.app_meta enable row level security;

drop policy if exists "jobs_select_active" on public.jobs;

drop policy if exists "companies_select_official" on public.companies;

drop policy if exists "app_meta_select" on public.app_meta;
create policy "app_meta_select"
on public.app_meta for select
to anon, authenticated
using (true);

-- 注意：不要给 anon 插入/更新/删除策略；Admin 使用 service_role key 推送

-- Viewer 登录账号（Admin 推送；anon 不可 SELECT password_hash）
create table if not exists public.app_users (
  account text primary key,
  password_hash text not null,
  notes text,
  enabled boolean not null default true,
  expires_at date,
  updated_at timestamptz not null default now()
);

create index if not exists app_users_enabled_idx on public.app_users (enabled);

drop trigger if exists app_users_set_updated_at on public.app_users;
create trigger app_users_set_updated_at
before update on public.app_users
for each row execute function public.set_updated_at();

alter table public.app_users enable row level security;
-- 不给 anon/authenticated 任何 SELECT 策略；读写走 service_role 或 SECURITY DEFINER RPC

-- PBKDF2-HMAC-SHA256（与 Python app.auth.password 格式 pbkdf2_sha256$iter$salt$hex 对齐）
create or replace function public.bytea_xor(a bytea, b bytea)
returns bytea
language plpgsql
immutable
as $$
declare
  i int;
  la int;
  lb int;
  out bytea := decode(repeat('00', greatest(length(a), length(b))), 'hex');
begin
  la := length(a);
  lb := length(b);
  for i in 0..greatest(la, lb) - 1 loop
    out := set_byte(
      out,
      i,
      get_byte(a, least(i, la - 1)) # get_byte(b, least(i, lb - 1))
    );
  end loop;
  return out;
end;
$$;

create or replace function public.pbkdf2_sha256(
  p_password text,
  p_salt text,
  p_iterations int,
  p_dklen int default 32
)
returns bytea
language plpgsql
immutable
as $$
declare
  hlen int := 32;
  block_count int;
  i int;
  j int;
  u bytea;
  t bytea;
  block bytea;
  result bytea := ''::bytea;
begin
  block_count := ceil(p_dklen::numeric / hlen);
  for i in 1..block_count loop
    block := decode(lpad(to_hex(i), 8, '0'), 'hex');
    u := hmac(
      convert_to(p_salt, 'UTF8') || block,
      convert_to(p_password, 'UTF8'),
      'sha256'
    );
    t := u;
    for j in 2..p_iterations loop
      u := hmac(u, convert_to(p_password, 'UTF8'), 'sha256');
      t := public.bytea_xor(t, u);
    end loop;
    if i = block_count then
      result := result || substring(t from 1 for p_dklen - (block_count - 1) * hlen);
    else
      result := result || t;
    end if;
  end loop;
  return result;
end;
$$;

create or replace function public.viewer_login(p_account text, p_password text)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  stored text;
  parts text[];
  iterations int;
  salt text;
  expected_hex text;
  computed_hex text;
begin
  if p_account is null or btrim(p_account) = '' or p_password is null then
    return false;
  end if;
  select password_hash into stored
  from public.app_users
  where account = btrim(p_account)
    and enabled = true
    and (expires_at is null or expires_at >= current_date);
  if stored is null then
    return false;
  end if;
  parts := string_to_array(stored, '$');
  if array_length(parts, 1) <> 4 or parts[1] <> 'pbkdf2_sha256' then
    return false;
  end if;
  iterations := parts[2]::int;
  salt := parts[3];
  expected_hex := parts[4];
  computed_hex := encode(
    public.pbkdf2_sha256(p_password, salt, iterations, 32),
    'hex'
  );
  return lower(computed_hex) = lower(expected_hex);
end;
$$;

create or replace function public.viewer_has_accounts()
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists(
    select 1 from public.app_users
    where enabled = true and (expires_at is null or expires_at >= current_date)
  );
$$;

create table if not exists public.viewer_sessions (
  token_hash text primary key,
  account text not null references public.app_users(account) on update cascade on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  last_used_at timestamptz not null default now()
);

create index if not exists viewer_sessions_account_idx
on public.viewer_sessions(account);
create index if not exists viewer_sessions_expires_idx
on public.viewer_sessions(expires_at);

create table if not exists public.viewer_login_attempts (
  account_key text primary key,
  failed_count integer not null default 0,
  window_started_at timestamptz not null default now(),
  locked_until timestamptz
);

alter table public.viewer_sessions enable row level security;
alter table public.viewer_login_attempts enable row level security;

create or replace function public.viewer_login_session(
  p_account text,
  p_password text
)
returns table(session_token text, session_account text, expires_at timestamptz)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_account text := btrim(coalesce(p_account, ''));
  v_now timestamptz := now();
  v_failed integer := 0;
  v_window timestamptz;
  v_locked timestamptz;
  v_token text;
  v_expires timestamptz;
begin
  if v_account = '' or p_password is null then
    return;
  end if;

  if not exists (
    select 1 from public.app_users u
    where u.account = v_account
      and u.enabled = true
      and (u.expires_at is null or u.expires_at >= current_date)
  ) then
    return;
  end if;

  select a.failed_count, a.window_started_at, a.locked_until
  into v_failed, v_window, v_locked
  from public.viewer_login_attempts a
  where a.account_key = v_account;

  if v_locked is not null and v_locked > v_now then
    return;
  end if;

  if not public.viewer_login(v_account, p_password) then
    if v_window is null or v_window < v_now - interval '15 minutes' then
      v_failed := 1;
      v_window := v_now;
    else
      v_failed := coalesce(v_failed, 0) + 1;
    end if;
    insert into public.viewer_login_attempts(
      account_key, failed_count, window_started_at, locked_until
    ) values (
      v_account,
      v_failed,
      v_window,
      case when v_failed >= 5 then v_now + interval '15 minutes' else null end
    )
    on conflict (account_key) do update set
      failed_count = excluded.failed_count,
      window_started_at = excluded.window_started_at,
      locked_until = excluded.locked_until;
    return;
  end if;

  delete from public.viewer_login_attempts where account_key = v_account;
  delete from public.viewer_sessions
  where account = v_account or expires_at <= v_now;

  v_token := encode(gen_random_bytes(32), 'hex');
  v_expires := v_now + interval '12 hours';
  insert into public.viewer_sessions(token_hash, account, created_at, expires_at, last_used_at)
  values (
    encode(digest(v_token, 'sha256'), 'hex'),
    v_account,
    v_now,
    v_expires,
    v_now
  );

  return query select v_token, v_account, v_expires;
end;
$$;

create or replace function public.viewer_sync_jobs(
  p_session_token text,
  p_updated_at timestamptz default null,
  p_after_id uuid default null,
  p_limit integer default 1000
)
returns setof public.jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_account text;
  v_now timestamptz := now();
begin
  select s.account into v_account
  from public.viewer_sessions s
  join public.app_users u
    on u.account = s.account
   and u.enabled = true
   and (u.expires_at is null or u.expires_at >= current_date)
  where s.token_hash = encode(digest(coalesce(p_session_token, ''), 'sha256'), 'hex')
    and s.expires_at > v_now;

  if v_account is null then
    raise exception using errcode = '28000', message = 'viewer session expired';
  end if;

  update public.viewer_sessions
  set last_used_at = v_now
  where token_hash = encode(digest(p_session_token, 'sha256'), 'hex');

  return query
  select j.*
  from public.jobs j
  where j.status in ('active', 'deleted')
    and (
      p_updated_at is null
      or (p_after_id is null and j.updated_at >= p_updated_at)
      or (
        p_after_id is not null
        and (j.updated_at > p_updated_at or (j.updated_at = p_updated_at and j.id > p_after_id))
      )
    )
  order by j.updated_at asc, j.id asc
  limit least(greatest(coalesce(p_limit, 1000), 1), 1000);
end;
$$;

revoke all on function public.viewer_login(text, text) from public, anon, authenticated;
revoke all on function public.viewer_login_session(text, text) from public;
revoke all on function public.viewer_sync_jobs(text, timestamptz, uuid, integer) from public;
grant execute on function public.viewer_login_session(text, text) to anon, authenticated;
grant execute on function public.viewer_sync_jobs(text, timestamptz, uuid, integer) to anon, authenticated;
grant execute on function public.viewer_has_accounts() to anon, authenticated;
