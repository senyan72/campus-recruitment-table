-- Existing Supabase projects may have been created with an older schema.
-- Run this file in the Supabase SQL Editor. It is safe to run repeatedly.

create extension if not exists "pgcrypto";

-- Columns used by the current local-to-cloud job payload.
alter table if exists public.jobs
  add column if not exists group_name text;
alter table if exists public.jobs
  add column if not exists salary_range text;
alter table if exists public.jobs
  add column if not exists headcount text;

-- Normalize the three supported job types before replacing the old check.
update public.jobs
set recruit_bucket = '日常实习'
where recruit_bucket in ('暑期实习', '暑假实习', '寒假实习');

update public.jobs
set recruit_bucket = '应届生实习'
where recruit_bucket in ('日常实习', '应届实习')
  and (
    recruit_project like '%应届%实习%'
    or recruit_project like '%校招%实习%'
    or recruit_project like '%校园%实习%'
    or recruit_project like '%毕业%实习%'
    or title like '%届%实习%'
    or title like '%实习%届%'
  );

alter table if exists public.jobs
  drop constraint if exists jobs_recruit_bucket_check;
alter table if exists public.jobs
  add constraint jobs_recruit_bucket_check
  check (recruit_bucket is null or recruit_bucket in ('校招', '应届生实习', '日常实习'));

-- Admin account synchronization table.
create table if not exists public.app_users (
  account text primary key,
  password_hash text not null,
  notes text,
  enabled boolean not null default true,
  expires_at date,
  updated_at timestamptz not null default now()
);

alter table public.app_users
  add column if not exists expires_at date;

create index if not exists app_users_enabled_idx
  on public.app_users (enabled);

-- Keep account timestamps consistent with the main schema when the helper exists.
do $$
begin
  if to_regprocedure('public.set_updated_at()') is not null then
    execute 'drop trigger if exists app_users_set_updated_at on public.app_users';
    execute 'create trigger app_users_set_updated_at before update on public.app_users for each row execute function public.set_updated_at()';
  end if;
end
$$;

alter table public.app_users enable row level security;

-- PBKDF2-HMAC-SHA256 compatible with app.auth.password.
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

grant execute on function public.viewer_login(text, text) to anon, authenticated;
grant execute on function public.viewer_has_accounts() to anon, authenticated;

-- Do not grant anon direct read/write access to app_users. Admin uses
-- service_role; Viewer calls only the SECURITY DEFINER RPCs above.
