-- Viewer access-license expiry. Run after the existing account/session migrations.
alter table public.app_users
  add column if not exists expires_at date;

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
      v_account, v_failed, v_window,
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
  values (encode(digest(v_token, 'sha256'), 'hex'), v_account, v_now, v_expires, v_now);
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
