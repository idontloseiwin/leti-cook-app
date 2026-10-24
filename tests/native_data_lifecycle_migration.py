"""Schema26 ->27 exact proof and atomic rejection on a disposable PostgreSQL17."""
from pathlib import Path
from datetime import timedelta
import json,time,sys
import psycopg
from psycopg.rows import dict_row
from schema25_regressions.fixtures import dsns,admission,Fixture
from ops.schema_manifest import validate_schema_bundle
from ops.schema_upgrade import build_upgrade_sql
from db.hot.legacy_source_provenance import legacy_update_copy_window_sql
from db.hot.update_copy_sources import UPDATE_COPY_WINDOW_SQL

ROOT=Path(__file__).resolve().parents[1]
CHANNEL='UC'+'m'*22
VIDEO='migrate0001'
UNKNOWN='migrate0002'

def main():
    started=time.monotonic();checks=[]
    def check(ok,name):
        if not ok:raise AssertionError(name)
        checks.append(name)
    admin_dsn,runtime_dsn=dsns()
    files={p.relative_to(ROOT).as_posix():p.read_bytes() for p in (ROOT/'schema').rglob('*') if p.is_file()}
    files['application/schema-release.json']=(ROOT/'application/schema-release.json').read_bytes()
    plan=validate_schema_bundle(files)
    def upgrade():
        with psycopg.connect(admin_dsn,autocommit=True) as upgrade_conn:
            with upgrade_conn.cursor() as cursor:
                cursor.execute(build_upgrade_sql(plan,expected_current_version=26))
                while cursor.nextset():pass
    with psycopg.connect(admin_dsn,autocommit=True,row_factory=dict_row) as conn:
        def row(sql,args=None):return conn.execute(sql,args).fetchone()
        def execute(sql,args=None):conn.execute(sql,args)
        check(row('SELECT current_version FROM operator_private.schema_state')['current_version']==26,'starts_schema26')
        execute("SET statement_timeout='20s'; SET timezone='UTC'; SET jit=off")
        domain_tables=('public.video_type_review_cases','public.video_type_review_history','public.video_inventory_memberships',
            'public.video_collection_state','public.videos','public.video_inventory','public.channel_collection_state','public.channels')
        def domain_snapshot():
            return {table:row("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') AS rows FROM "+table+' t')['rows'] for table in domain_tables}
        domain_before=domain_snapshot()
        check(all(not rows for rows in domain_before.values()),'fresh_domain_empty')
        at=row('SELECT statement_timestamp() AS at')['at']
        old=at-timedelta(days=2);new=at-timedelta(days=1);publication=at-timedelta(days=200)
        execute('INSERT INTO public.channels(channel_id) VALUES(%s)',(CHANNEL,))
        for video in (VIDEO,UNKNOWN):
            execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at) VALUES(%s,%s,'stream',%s)",(video,CHANNEL,publication))
        execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
            source_published_at,first_seen_at,last_seen_at,last_present_at)
            VALUES(%s,%s,'stream',%s,%s,%s,%s,%s)""",(VIDEO,CHANNEL,'UULV'+CHANNEL[2:],publication,old,old,old))
        execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
            typed_source_published_at,typed_stream_actual_end_at,typed_collection_status,next_due_at)
            VALUES(%s,%s,'stream',%s,%s,'pending',%s)""",(VIDEO,CHANNEL,publication,publication+timedelta(hours=1),at+timedelta(days=20)))
        execute("""INSERT INTO public.videos(video_id,channel_id,published_at,stream_actual_end_at,metadata_checked_at)
            VALUES(%s,%s,%s,%s,%s)""",(VIDEO,CHANNEL,publication,publication+timedelta(hours=1),new))
        execute("""INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,
            observed_types,observed_channels,state) VALUES(%s,%s,'stream',ARRAY['stream'],ARRAY[%s],'open')""",(VIDEO,CHANNEL,CHANNEL))
        def retained():
            return {table:row("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') AS rows FROM "+table+' t')['rows'] for table in ('public.video_inventory','public.video_collection_state','public.video_type_review_cases','operator_private.schema_state','operator_private.schema_migrations')}
        before=retained()
        legacy=legacy_update_copy_window_sql()
        check(row(legacy)['invalid_source_clock'],'unknown_copy_refuses_before_upgrade')
        try:upgrade()
        except psycopg.Error as error:
            check('legacy_source_provenance_unconfirmed' in str(error),'migration_unknown_origin_refuses')
        else:raise AssertionError('unknown_origin_was_accepted')
        check(retained()==before,'failed_upgrade_preserves_all_rows_and_ledger')
        check(row("SELECT NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='video_inventory' AND column_name='published_acquired_at') AS ok")['ok'],'failed_upgrade_rolls_back_columns')
        # Synthetic fixture now receives the missing exact observation; no clocks
        # or payloads in the guarded rows are changed to manufacture an origin.
        execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
            source_published_at,first_seen_at,last_seen_at,last_present_at)
            VALUES(%s,%s,'stream',%s,%s,%s,%s,%s)""",(UNKNOWN,CHANNEL,'UULV'+CHANNEL[2:],publication,new,new,new))
        check(not row(legacy)['invalid_source_clock'],'early_window_valid')
        # A real pending queue row appears between the two observations.
        execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
            typed_source_published_at,typed_collection_status,next_due_at)
            VALUES(%s,%s,'stream',%s,'pending',%s)""",(UNKNOWN,CHANNEL,publication,at+timedelta(days=10)))
        final_window=row(legacy)
        check(not final_window['invalid_source_clock'],'pending_valid_without_typed_result_clock')
        due=row('SELECT next_due_at FROM public.video_collection_state WHERE video_id=%s',(VIDEO,))['next_due_at']
        upgrade()
        check(row('SELECT current_version FROM operator_private.schema_state')['current_version']==27,'upgrade_committed_schema27')
        check(row('SELECT published_acquired_at FROM public.video_inventory WHERE video_id=%s',(VIDEO,))['published_acquired_at']==old,'canonical_earliest_exact_observation')
        q=row('SELECT * FROM public.video_collection_state WHERE video_id=%s',(VIDEO,))
        check(q['typed_source_published_acquired_at']==old and q['typed_source_stream_end_acquired_at']==new,'queue_field_specific_origins')
        check(q['next_due_at']==due and q['typed_collection_checked_at'] is None and q['typed_collection_status']=='pending','migration_preserves_schedule_and_pending')
        check(row('SELECT evidence_expires_at FROM public.video_type_review_cases WHERE video_id=%s',(VIDEO,))['evidence_expires_at']==old+timedelta(hours=696),'review_inherits_witness_deadline')
        check(row('SELECT typed_source_published_acquired_at FROM public.video_collection_state WHERE video_id=%s',(UNKNOWN,))['typed_source_published_acquired_at']==new,'new_pending_migrates_same_proof')
        actual=row(UPDATE_COPY_WINDOW_SQL)
        check({k:v for k,v in actual.items() if k!='as_of'}=={k:v for k,v in final_window.items() if k!='as_of'},'old_reader_equals_new_reader')
        # Remove only this synthetic fixture; capability assertion is identical
        # to fresh-schema native checks and performs no provider requests.
        with conn.transaction():
            execute('DELETE FROM public.video_type_review_cases WHERE video_id=%s',(VIDEO,))
            for table in ('public.video_collection_state','public.videos','public.video_inventory_memberships','public.video_inventory'):
                execute('DELETE FROM '+table+' WHERE video_id=ANY(%s)',([VIDEO,UNKNOWN],))
            execute('DELETE FROM public.channels WHERE channel_id=%s',(CHANNEL,))
        check(domain_snapshot()==domain_before,'synthetic_rows_restored_exact')
        from tools import schema_catalog_snapshot as catalog
        snapshot=catalog.capture(lambda sql:json.loads(next(iter(row(sql).values()))))
        Path('/out/catalog.json').write_bytes(catalog.serialize(snapshot))
    with admission() as gate:
        f=Fixture(admin_dsn,runtime_dsn,gate)
        try:f.observe()
        finally:f.close()
    result=dict(status='passed',checks=checks,seconds=round(time.monotonic()-started,3),fixture_rows_restored=True)
    Path('/out/RESULT.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':main()
