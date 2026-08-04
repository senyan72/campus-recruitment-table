-- Viewer 增量同步需要读取 deleted 墓碑，才能从本地撤下云端已删除岗位。
-- 同时保留 active 只读；写操作仍仅由 service_role 执行。

alter table public.jobs enable row level security;

drop policy if exists "jobs_select_active" on public.jobs;
create policy "jobs_select_active"
on public.jobs for select
to anon, authenticated
using (status in ('active', 'deleted'));
