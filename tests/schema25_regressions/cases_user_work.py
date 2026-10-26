"""Current restore/Saved owner cases with independent backup document fixtures."""
from db.user_work.user_work_backup import UserWorkBackupRepository
from db.user_work.saved_collections import SavedCollectionsRepository
from db.base.postgres import DatabaseError
from .fixtures import CHANNEL,fixture_json,expect_error

def restore_merge(f):
    repo=UserWorkBackupRepository(f.database)
    f.check(repo.import_document(fixture_json(f,'restore_merge_expression.sql'))['outcome']=='ready','restore_import')
    f.channel();identity='a0000000-0000-4000-8000-000000000001'
    result=repo.bind_pending(identity,1,CHANNEL,{'channel_id':CHANNEL})
    f.check(result['outcome']=='ready' and result['merged'] is True,'tracked_merge')
    checks=[
      "SELECT folder_id='f0000000-0000-4000-8000-000000000001'::uuid AND parent_channel_id IS NULL AND NOT is_hidden FROM public.channels_with_control WHERE channel_id=%s",
      "SELECT EXISTS(SELECT 1 FROM public.channel_label_assignments WHERE channel_id=%s AND label_key='monetized')",
      "SELECT EXISTS(SELECT 1 FROM public.saved_objects WHERE kind='view' AND id='e0000000-0000-4000-8000-000000000001' AND channel_id=%s)",
      "SELECT EXISTS(SELECT 1 FROM public.saved_collection_members WHERE user_channel_ref=(SELECT user_work_id FROM public.channels WHERE channel_id=%s) AND restore_owner_ref IS NULL)",
      "SELECT EXISTS(SELECT 1 FROM public.user_channel_restore_items WHERE id='b0000000-0000-4000-8000-000000000002' AND parent_ref=(SELECT user_work_id FROM public.channels WHERE channel_id=%s) AND revision=2)"]
    for sql in checks:f.check(f.scalar(sql,(CHANNEL,)) is True,'merge_owned_state')
    f.check(f.scalar('SELECT NOT EXISTS(SELECT 1 FROM public.user_channel_restore_items WHERE id=%s::uuid)',(identity,)) is True,'pending_removed')
    f.check(repo.bind_pending(identity,1,CHANNEL,{})['outcome']=='not_found','bind_replay')
    f.reset();f.check(repo.import_document(fixture_json(f,'restore_parent_cycle_expression.sql'))['outcome']=='ready','cycle_import');f.channel()
    f.rows("INSERT INTO public.channels(channel_id,title,user_work_id,parent_channel_id,initial_long_inventory_status) VALUES('UCparentparentparentpar1','Parent','d0000000-0000-4000-8000-000000000004',%s,'completed')",(CHANNEL,))
    f.rows("DELETE FROM public.user_channel_restore_items WHERE id='d0000000-0000-4000-8000-000000000004'")
    f.check(repo.bind_pending('a0000000-0000-4000-8000-000000000003',1,CHANNEL,{'channel_id':CHANNEL})['outcome']=='ready','cycle_merge')
    f.check(f.scalar('SELECT parent_channel_id IS NULL FROM public.channels WHERE channel_id=%s',(CHANNEL,)) is True,'cycle_avoided')
    f.check(f.scalar(checks[1],(CHANNEL,)) is True,'cycle_label_preserved')

def saved_noop_revision(f):
    identity='c0000000-0000-4000-8000-000000000001';repo=SavedCollectionsRepository(f.database)
    f.rows("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint,revision,member_count) VALUES('collection',%s::uuid,'No-op check',repeat('0',64),1,0)",(identity,))
    def change(index,action,ids,revision):
        return repo.change_members(identity,f'd0000000-0000-4000-8000-{index:012d}',action,ids,revision,'a'*64)
    value=change(1,'add',['aaaaaaaaaaa','bbbbbbbbbbb'],1)
    f.check(value['mutation']['applied_revision']==2 and value['mutation']['changed_count']==2,'add_two')
    value=change(2,'add',['aaaaaaaaaaa','bbbbbbbbbbb'],2);before=f.snapshot()
    f.check(value['mutation']['changed_count']==0 and value['mutation']['applied_revision']==2 and value['collection']['revision']==2,'duplicate_noop')
    value=change(2,'add',['aaaaaaaaaaa','bbbbbbbbbbb'],2)
    f.check(value['mutation']['replayed'] is True and value['mutation']['applied_revision']==2 and f.snapshot()==before,'receipt_replay')
    f.check(change(3,'remove',['ccccccccccc'],2)['mutation']['applied_revision']==2,'absent_remove_noop')
    before=f.snapshot();f.check(change(4,'remove',['aaaaaaaaaaa'],1)['outcome']=='conflict','stale_conflict');f.check(f.snapshot()==before,'conflict_no_writes')
    value=change(5,'remove',['aaaaaaaaaaa'],2)
    f.check(value['mutation']['applied_revision']==3 and value['mutation']['changed_count']==1,'real_remove')
    f.check(f.scalar('SELECT member_count FROM public.saved_objects WHERE id=%s::uuid',(identity,))==1,'one_member')
    f.check(f.scalar('SELECT count(*) FROM public.saved_collection_commands')==4,'exact_receipts')

def folder_label_management(f):
    repo=UserWorkBackupRepository(f.database)
    f.check(repo.import_document(fixture_json(f,'labels_renamed_preset_expression.sql'))['outcome']=='ready','renamed_import')
    f.check(f.scalar("SELECT name FROM public.channel_labels WHERE label_key='monetized'")=='Paid','renamed_preset')
    f.check(f.scalar("SELECT label_type FROM public.channel_labels WHERE name='Monetized'")=='custom','custom_old_name')
    f.reset();f.check(repo.import_document(fixture_json(f,'labels_deleted_preset_expression.sql'))['outcome']=='ready','deleted_preset_import')
    f.check(f.scalar("SELECT NOT EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key='monetized')") is True,'preset_stays_deleted')
    f.reset();expect_error(f,lambda:repo.import_document(fixture_json(f,'labels_invalid_version_expression.sql')),DatabaseError,code='P0001')

def user_work_limit_race(f):
    # Two writers at the folder limit: the second waits for the first to commit,
    # then counts its row and is refused, so a backup stays exportable.
    import threading
    import psycopg
    from .fixtures import dsns
    f.rows("INSERT INTO public.folders(name) SELECT 'race '||v FROM generate_series(1,999) v")
    admin_dsn=dsns()[0]
    first=psycopg.connect(admin_dsn,connect_timeout=5);second=psycopg.connect(admin_dsn,connect_timeout=5)
    outcome={}
    try:
        first.execute("SET lock_timeout='10s'");second.execute("SET lock_timeout='10s'")
        first.execute("INSERT INTO public.folders(name) VALUES ('race first')")
        def insert_second():
            try:
                second.execute("INSERT INTO public.folders(name) VALUES ('race second')");second.commit()
                outcome['second']='committed'
            except psycopg.Error as error:
                second.rollback();outcome['second']=error.diag.message_primary
        worker=threading.Thread(target=insert_second);worker.start()
        worker.join(1.0);waited=worker.is_alive()
        first.commit();worker.join(15)
        f.check(waited and outcome.get('second')=='user_work_limit_reached','limit_race_second_refused')
        f.check(f.scalar('SELECT count(*) FROM public.folders')==1000,'limit_race_exact_cap')
    finally:
        first.close();second.close()
        f.rows("DELETE FROM public.folders WHERE name LIKE 'race %%'")
