"""Strict final-schema catalog contract: nineteen intrinsic hooks and forty-two triggers.

No stored workflow calls, source data reads, writes or permissive old-schema mode.
Runtime verifies the delivered integrity definitions; the fresh installer
separately verifies schema checksums. No version-only acceptance or new authority.
"""
from .capability_facts import require_boolean_facts

READINESS_SQL = r"""
WITH expected_functions(signature,body_md5,security_definer,search_path,service_execute) AS (VALUES
('public.alert_duration_clock()','e54d6f1a50d490bcb2ad39d8b013f3ef',false,'search_path=pg_catalog, pg_temp',false),
('public.alert_visibility_clock()','ee4d0e897b43641386622c7938395082',false,'search_path=pg_catalog, pg_temp',false),
('public.alert_folder_clock()','6a5d202db18417491edce0ea6d719d72',false,'search_path=pg_catalog, pg_temp',false),
('public.capture_alert_observations()','f905357f9f669ae925658ad468199b0f',false,'search_path=pg_catalog, pg_temp',false),
('operator_private.serialize_user_work_bytes()','fe0c903983423110523eb920d246fcca',true,'search_path=pg_catalog, public, pg_temp',false),
('operator_private.check_user_work_bytes()','4780a63b1d8376858f42608603cb2a9e',true,'search_path=pg_catalog, public, pg_temp',false),
('public.bind_channel_user_work_owner()','fad6ef72f002d88375084c6ca04f2bab',true,'search_path=pg_catalog, public, pg_temp',true),
('public.enforce_user_work_backup_limits()','3a8712eebb37289261a22a2b91d5bbf0',false,'search_path=pg_catalog, public, pg_temp',false),
('public.enforce_video_inventory_membership_scan()','dec99f98b218890a8c0654828f32dac4',false,'search_path=pg_catalog, public, pg_temp',false),
('public.guard_channel_user_work_identity()','b24fa9878daa2989919ff4f960dc891f',false,'search_path=pg_catalog, pg_temp',false),
('public.guard_hot_operator_scope()','eda8054b15f84795ee8637fbcd457d44',true,'search_path=pg_catalog, public, operator_private, pg_temp',true),
('public.guard_hot_operator_target_scope()','6d224d5075e65a755c8d21f5ecc3efb6',true,'search_path=pg_catalog, public, operator_private, pg_temp',true),
('public.guard_hot_storage_collection_delete()','c2e1936914e574486ec3ab1ad137a920',true,'search_path=pg_catalog, public, pg_temp',false),
('public.guard_inventory_chain_identity()','41a4ae471fec205764ff7297df39d1e7',false,'search_path=pg_catalog, public, pg_temp',false),
('public.guard_inventory_chain_parent_version()','a3d0878943b715db20fe7e67e1a2337f',false,'search_path=pg_catalog, public, pg_temp',false),
('public.guard_inventory_chain_consistency()','1571f968fcb209904f9fb6a57351632b',false,'search_path=pg_catalog, public, pg_temp',false),
('public.guard_manual_video_type_decision()','6fb0c0250c6ade3c1216d0f93c5fa2ba',true,'search_path=pg_catalog, public, pg_temp',false),
('public.queue_video_search_projection()','4f05611bac9bbb932fda3be3dc23353c',true,'search_path=pg_catalog, pg_temp',false),
('public.track_video_search_inventory()','c91a70da80050a353b6575c0299a40df',true,'search_path=pg_catalog, pg_temp',false)), expected_triggers(table_name,trigger_name,signature,type_bits,columns,deferred_constraint) AS (VALUES
('public.folders','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.channel_labels','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.channel_label_assignments','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.saved_objects','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.saved_collection_members','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.user_channel_restore_items','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.video_type_review_history','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY[]::text[],false),
('public.channels','user_work_bytes_before','operator_private.serialize_user_work_bytes()',22,ARRAY['user_input','folder_id','parent_channel_id']::text[],false),
('public.video_type_review_cases','user_work_bytes_before','operator_private.serialize_user_work_bytes()',18,ARRAY['decision_type','decision_reason','decision_at','decision_revision']::text[],false),
('public.video_type_review_cases','user_work_bytes_decision_insert','operator_private.serialize_user_work_bytes()',7,ARRAY[]::text[],false),
('operator_private.user_work_bytes_pending','user_work_bytes_commit','operator_private.check_user_work_bytes()',5,ARRAY[]::text[],true),
('public.operator_job_inventory_chains','inventory_chain_identity_guard','public.guard_inventory_chain_identity()',19,ARRAY[]::text[],false),
('public.operator_jobs','inventory_chain_parent_version_guard','public.guard_inventory_chain_parent_version()',23,ARRAY[]::text[],false),
('public.operator_job_inventory_chains','inventory_chain_children_consistency_guard','public.guard_inventory_chain_consistency()',29,ARRAY[]::text[],true),
('public.operator_jobs','inventory_chain_parent_consistency_guard','public.guard_inventory_chain_consistency()',21,ARRAY[]::text[],true),
('public.video_type_review_cases','review_case_channel_owner','public.bind_channel_user_work_owner()',23,ARRAY['channel_id','user_channel_ref']::text[],false),
('public.saved_collection_members','saved_member_channel_owner','public.bind_channel_user_work_owner()',23,ARRAY['video_id','user_channel_ref']::text[],false),
('public.channel_label_assignments','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.channel_labels','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.channels','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.folders','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.saved_collection_members','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.saved_objects','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.video_type_review_history','user_work_backup_limits','public.enforce_user_work_backup_limits()',4,ARRAY[]::text[],false),
('public.video_inventory_memberships','video_inventory_membership_scan_guard','public.enforce_video_inventory_membership_scan()',23,ARRAY['channel_id','video_type','membership_status','first_seen_scan_id','last_seen_scan_id','absence_confirmed_by_scan_id']::text[],false),
('public.channels','channels_user_work_identity_immutable','public.guard_channel_user_work_identity()',19,ARRAY['user_work_id']::text[],false),
('public.operator_jobs','hot_operator_source_scope','public.guard_hot_operator_scope()',23,ARRAY[]::text[],false),
('public.operator_job_targets','hot_operator_target_scope','public.guard_hot_operator_target_scope()',23,ARRAY[]::text[],false),
('public.video_collection_state','hot_storage_collection_delete_guard','public.guard_hot_storage_collection_delete()',11,ARRAY[]::text[],false),
('public.channel_playlist_state','hot_storage_playlist_delete_guard','public.guard_hot_storage_collection_delete()',11,ARRAY[]::text[],false),
('public.video_inventory','video_type_review_canonical_guard','public.guard_manual_video_type_decision()',19,ARRAY['video_id','video_type','channel_id']::text[],false),
('public.video_inventory','video_inventory_search_projection','public.track_video_search_inventory()',21,ARRAY['channel_id']::text[],false),
('public.video_inventory','video_inventory_search_queue','public.queue_video_search_projection()',29,ARRAY[]::text[],false),
('public.video_inventory_memberships','video_membership_search_queue','public.queue_video_search_projection()',29,ARRAY[]::text[],false),
('public.video_type_review_cases','video_review_search_queue','public.queue_video_search_projection()',29,ARRAY[]::text[],false),
('public.videos','videos_search_queue','public.queue_video_search_projection()',29,ARRAY['video_id','channel_id','title','published_at','metadata_checked_at','duration','publication_status']::text[],false)), expected_alert_triggers(table_name,trigger_name,signature,type_bits,columns,nargs,args_md5,new_table,when_expression) AS (VALUES
('public.videos','alert_duration_clock','public.alert_duration_clock()',19,ARRAY['duration']::text[],0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'old.durationisdistinctfromnew.duration'),
('public.channels','alert_folder_clock','public.alert_folder_clock()',19,ARRAY['folder_id']::text[],0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'old.folder_idisdistinctfromnew.folder_id'),
('public.channel_collection_state','alert_visibility_clock','public.alert_visibility_clock()',19,ARRAY['is_hidden']::text[],0,'d41d8cd98f00b204e9800998ecf8427e',NULL,'old.is_hiddenisdistinctfromnew.is_hidden'),
('public.video_snapshots','alert_video_observations','public.capture_alert_observations()',4,ARRAY[]::text[],2,'90ac35d9150081de9c638a9edb3af860','inserted',NULL),
('public.channel_snapshots','alert_channel_observations','public.capture_alert_observations()',4,ARRAY[]::text[],2,'7c0a12b32ae2c2ad1e4eb9f6460b31fd','inserted',NULL),
('public.video_availability_events','alert_availability_observations','public.capture_alert_observations()',4,ARRAY[]::text[],2,'613011175a60ea5f580ec32a85c39cf3','inserted',NULL)), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='postgres') AS owner_oid,
 (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 (SELECT count(*)=3 FROM pg_catalog.pg_roles WHERE rolname IN ('anon','authenticated','service_role'))
 AND coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), functions AS MATERIALIZED (
 -- Catalog identity lookup must also work for lic_reader, which deliberately
 -- has no USAGE on operator_private. reg* name resolution checks that USAGE.
 SELECT p.*, n.nspname||'.'||p.proname||'('||pg_catalog.oidvectortypes(p.proargtypes)||')' AS signature
 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname IN ('public','operator_private')
), triggers AS MATERIALIZED (
 SELECT t.*, n.nspname||'.'||c.relname AS table_name
 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname IN ('public','operator_private') AND NOT t.tgisinternal
)
SELECT coalesce(roles.valid,false) AS roles_valid,
 ((SELECT count(*) FROM functions)=19 AND NOT EXISTS(
  SELECT 1 FROM expected_functions expected LEFT JOIN functions p ON p.signature=expected.signature
  WHERE p.oid IS NULL OR p.proowner IS DISTINCT FROM roles.owner_oid
   OR p.prokind<>'f' OR p.prorettype<>'trigger'::regtype OR p.proretset OR p.pronargs<>0
   OR p.pronargdefaults<>0 OR p.proargtypes<>''::oidvector OR p.proallargtypes IS NOT NULL
   OR p.proargmodes IS NOT NULL OR p.proargnames IS NOT NULL OR p.proargdefaults IS NOT NULL
   OR p.prosecdef IS DISTINCT FROM expected.security_definer OR p.provolatile<>'v'
   OR p.proisstrict OR p.proleakproof OR p.proparallel<>'u'
   OR p.provariadic<>0 OR p.prosupport<>0 OR p.procost<>100 OR p.prorows<>0
   OR (SELECT lanname FROM pg_catalog.pg_language WHERE oid=p.prolang)<>'plpgsql'
   OR p.proconfig IS DISTINCT FROM ARRAY[expected.search_path]
   OR md5(replace(replace(p.prosrc,E'\r\n',E'\n'),E'\r',E'\n'))<>expected.body_md5
   OR pg_catalog.has_function_privilege('service_role',p.oid,'EXECUTE') IS DISTINCT FROM expected.service_execute
   OR pg_catalog.has_function_privilege('anon',p.oid,'EXECUTE')
   OR pg_catalog.has_function_privilege('authenticated',p.oid,'EXECUTE')
   OR EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(p.proacl,pg_catalog.acldefault('f',p.proowner))) acl
    WHERE acl.grantee<>roles.owner_oid AND (acl.grantee IS DISTINCT FROM roles.service_oid
     OR NOT expected.service_execute OR acl.privilege_type<>'EXECUTE' OR acl.is_grantable))
 )) AS functions_valid,
 ((SELECT count(*) FROM triggers)=42 AND NOT EXISTS(
  SELECT 1 FROM expected_triggers expected LEFT JOIN triggers t ON t.table_name=expected.table_name
   AND t.tgname=expected.trigger_name
  LEFT JOIN pg_catalog.pg_constraint constraint_hook ON constraint_hook.oid=t.tgconstraint
  WHERE t.oid IS NULL OR t.tgfoid IS DISTINCT FROM
    (SELECT f.oid FROM functions f WHERE f.signature=expected.signature)
   OR t.tgenabled<>'O' OR t.tgtype<>expected.type_bits OR t.tgnargs<>0 OR t.tgargs<>'\x'::bytea
   OR t.tgqual IS NOT NULL OR t.tgoldtable IS NOT NULL OR t.tgnewtable IS NOT NULL
   OR t.tgparentid<>0
   OR CASE WHEN expected.deferred_constraint THEN
    (t.tgconstraint=0 OR NOT t.tgdeferrable OR NOT t.tginitdeferred
     OR constraint_hook.contype IS DISTINCT FROM 't'
     OR constraint_hook.conrelid IS DISTINCT FROM t.tgrelid
     OR constraint_hook.conname IS DISTINCT FROM t.tgname
     OR constraint_hook.condeferrable IS DISTINCT FROM true
     OR constraint_hook.condeferred IS DISTINCT FROM true)
    ELSE (t.tgconstraint<>0 OR t.tgdeferrable OR t.tginitdeferred) END
   OR ARRAY(SELECT a.attname::text FROM unnest(t.tgattr::smallint[]) WITH ORDINALITY cols(num,ord)
    JOIN pg_catalog.pg_attribute a ON a.attrelid=t.tgrelid AND a.attnum=cols.num
    WHERE NOT a.attisdropped ORDER BY cols.ord) IS DISTINCT FROM expected.columns
 ) AND NOT EXISTS(
  SELECT 1 FROM expected_alert_triggers expected LEFT JOIN triggers t ON t.table_name=expected.table_name
   AND t.tgname=expected.trigger_name
  WHERE t.oid IS NULL OR t.tgenabled<>'O' OR t.tgparentid<>0 OR t.tgconstraint<>0
   OR t.tgdeferrable OR t.tginitdeferred
   OR t.tgfoid IS DISTINCT FROM (SELECT f.oid FROM functions f WHERE f.signature=expected.signature)
   OR t.tgtype<>expected.type_bits OR t.tgnargs<>expected.nargs OR md5(t.tgargs)<>expected.args_md5
   OR t.tgoldtable IS NOT NULL OR t.tgnewtable::text IS DISTINCT FROM expected.new_table
   OR ARRAY(SELECT a.attname::text FROM unnest(t.tgattr::smallint[]) WITH ORDINALITY cols(num,ord)
    JOIN pg_catalog.pg_attribute a ON a.attrelid=t.tgrelid AND a.attnum=cols.num
    WHERE NOT a.attisdropped ORDER BY cols.ord) IS DISTINCT FROM expected.columns
   OR (t.tgqual IS NULL) IS DISTINCT FROM (expected.when_expression IS NULL)
   OR (expected.when_expression IS NOT NULL AND
       regexp_replace(lower(substring(pg_catalog.pg_get_triggerdef(t.oid,false)
         FROM ' WHEN \((.*)\) EXECUTE (?:FUNCTION|PROCEDURE) ')), '[()[:space:]]','','g')
       IS DISTINCT FROM expected.when_expression)
 )) AS triggers_valid
FROM roles
"""

def validate_readiness(row):
    require_boolean_facts(row, ('roles_valid', 'functions_valid', 'triggers_valid'))


def assert_final_schema_ready(database):
    validate_readiness(database.read_row('capability_final_schema',READINESS_SQL))
