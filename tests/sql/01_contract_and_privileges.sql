-- The bootstrapped schema passes its runtime contract, and the privilege model
-- the audit relies on holds. Runs on a fresh database; changes nothing.
begin;
do $$
begin
  assert not exists(
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname in ('public', 'operator_private')
      and (has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute'))),
    'a function is executable by anon or authenticated';
  assert not exists(
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname in ('public', 'operator_private') and p.prosecdef
      and not exists(select 1 from unnest(coalesce(p.proconfig, array[]::text[])) c(v)
                     where c.v like 'search_path=%')),
    'a SECURITY DEFINER function does not pin its search_path';
  assert not has_database_privilege('service_role', current_database(), 'TEMPORARY'),
    'service_role can create temporary objects';
  assert not exists(select 1 from pg_roles where rolname = 'supabase_admin'),
    'the unused supabase_admin role exists';
end $$;
-- Runtime readiness is exercised by the mandatory Python groups.
rollback;
