not exists(select 1 from pg_catalog.pg_class
         where oid=to_regclass('public.channel_collection_state') and relkind='r' and relrowsecurity
           and pg_catalog.pg_get_userbyid(relowner)='postgres')
      or not exists(select 1 from pg_catalog.pg_roles where rolname='service_role' and rolbypassrls and not rolsuper and not rolcanlogin)
      or not pg_catalog.has_table_privilege('service_role','public.channel_collection_state','SELECT')
      or pg_catalog.has_table_privilege('service_role','public.channel_collection_state','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
      or pg_catalog.has_any_column_privilege('service_role','public.channel_collection_state','REFERENCES')
      or exists(select 1 from unnest(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']) required(column_name)
           where not exists(select 1 from pg_catalog.pg_attribute a
             where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped and a.attname=required.column_name))
      or exists(select 1 from pg_catalog.pg_attribute a
           where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and pg_catalog.has_column_privilege('service_role',a.attrelid,a.attnum,'INSERT')
                 is distinct from (a.attname=ANY(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at'])))
      or exists(select 1 from pg_catalog.pg_attribute a
           where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and pg_catalog.has_column_privilege('service_role',a.attrelid,a.attnum,'UPDATE')
                 is distinct from (a.attname=ANY(ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id'])))
      or exists(select 1 from (VALUES ('anon'),('authenticated')) denied(role_name)
           where pg_catalog.has_table_privilege(denied.role_name,'public.channel_collection_state','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
              or pg_catalog.has_any_column_privilege(denied.role_name,'public.channel_collection_state','SELECT,INSERT,UPDATE,REFERENCES'))
      or exists(select 1 from pg_catalog.pg_class c
           cross join lateral pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('r',c.relowner))) acl
           where c.oid=to_regclass('public.channel_collection_state') and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.privilege_type<>'SELECT' or acl.is_grantable))
      or exists(select 1 from pg_catalog.pg_attribute a join pg_catalog.pg_class c on c.oid=a.attrelid
           cross join lateral pg_catalog.aclexplode(a.attacl) acl
           where c.oid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.is_grantable
               or not ((acl.privilege_type='UPDATE' and a.attname=ANY(ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']))
                    or (acl.privilege_type='INSERT' and a.attname=ANY(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at'])))))
      or not exists(select 1 from pg_catalog.pg_class c
           where c.oid=to_regclass('public.channel_collection_epoch_seq') and c.relkind='S'
             and pg_catalog.pg_get_userbyid(c.relowner)='postgres')
      or not pg_catalog.has_sequence_privilege('service_role','public.channel_collection_epoch_seq','USAGE')
      or pg_catalog.has_sequence_privilege('service_role','public.channel_collection_epoch_seq','SELECT,UPDATE')
      or exists(select 1 from (VALUES ('anon'),('authenticated')) denied(role_name)
           where pg_catalog.has_sequence_privilege(denied.role_name,'public.channel_collection_epoch_seq','USAGE,SELECT,UPDATE'))
      or exists(select 1 from pg_catalog.pg_class c
           cross join lateral pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('S',c.relowner))) acl
           where c.oid=to_regclass('public.channel_collection_epoch_seq') and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.privilege_type<>'USAGE' or acl.is_grantable))