"""Offline transaction protocol tests for the Saved mutation module."""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import sys
import unittest
from uuid import UUID

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.user_work import saved_mutations as mutations
from db.base.postgres import DatabaseServerError,DatabaseUnavailable

ID=UUID('c0000000-0000-4000-8000-000000000001')
REQUEST=UUID('d0000000-0000-4000-8000-000000000001')
CHANNEL='UC0000000000000000000001'
NOW=datetime(2026,10,5,tzinfo=timezone.utc)
FINGERPRINT='a'*64


def object_row(**updates):
    return dict(kind='collection',id=str(ID),name='Synthetic',channel_id=None,conditions=None,
        create_fingerprint=FINGERPRINT,revision=2,member_count=1,created_at=NOW.isoformat(),updated_at=NOW.isoformat(),deleted_at=None,**updates)


def args(**updates):
    result=dict(kind='collection',id=ID,name='Synthetic',expected_revision=2,fingerprint=FINGERPRINT,
        channel_id=None,conditions=None,name_clean=True,conditions_bytes=None,conditions_sql_null=True)
    result.update(updates);return result


def member_args(**updates):
    result=dict(id=ID,request_id=REQUEST,action='add',video_ids=['aaaaaaaaaaa'],expected_revision=2,fingerprint=FINGERPRINT)
    result.update(updates);return result


class Transaction:
    def __init__(self,*results):self.results=deque(results);self.calls=[];self.savepoints=[]
    def run(self,mode,sql,params=()):
        self.calls.append((mode,sql,tuple(params)))
        result=self.results.popleft()
        if isinstance(result,Exception):raise result
        return deepcopy(result)
    def fetch_one(self,sql,params=()):return self.run('read',sql,params)
    def returning_one(self,sql,params=()):return self.run('returning',sql,params)
    def execute(self,sql,params=()):return self.run('write',sql,params)
    def finalize_saved_collection_members(self,**values):
        self.calls.append(('finalize',mutations._FINALIZE_CHANGED_MEMBERS,dict(values)))
        result=self.results.popleft()
        if isinstance(result,Exception):raise result
        return deepcopy(result)
    @contextmanager
    def borrow(self):
        yield self
    @contextmanager
    def savepoint(self):
        self.savepoints.append('begin')
        try:yield self
        except BaseException:self.savepoints.append('rollback');raise
        else:self.savepoints.append('release')


class Database:
    def __init__(self,*results):self.tx=Transaction(*results);self.operations=[];self.ends=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self.tx
        except BaseException:self.ends.append('rollback');raise
        else:self.ends.append('commit')


def create(database,**kwargs):
    return mutations.create_saved_object(database,kind='collection',object_id=str(ID),name='Synthetic',fingerprint=FINGERPRINT,**kwargs)


def members(database,**kwargs):
    values=dict(collection_id=str(ID),request_id=str(REQUEST),action='add',video_ids=['aaaaaaaaaaa'],expected_revision=2,fingerprint=FINGERPRINT)
    values.update(kwargs);return mutations.change_saved_collection_members(database,**values)


class CreateTests(unittest.TestCase):
    def test_new_collection_advisory_lock_insert_savepoint_and_single_budget(self):
        row=object_row();db=Database(args(),{'locked':None},None,{'object':row})
        result=create(db)
        self.assertEqual(result['outcome'],'ready');self.assertNotIn('create_fingerprint',result['object'])
        self.assertEqual(len(db.operations),1);self.assertEqual(db.operations[0].total_timeout_ms,60000)
        self.assertFalse(db.operations[0].read_only);self.assertEqual(db.tx.savepoints,['begin','release'])
        self.assertIn("hashtextextended('saved-'",db.tx.calls[1][1]);self.assertIn(',0)',db.tx.calls[1][1])
        self.assertIn('FOR UPDATE',db.tx.calls[2][1]);self.assertEqual(db.tx.calls[3][0],'returning')
        self.assertNotIn(str(ID),db.tx.calls[1][1]);self.assertEqual(db.ends,['commit'])

    def test_create_replay_fingerprint_and_deleted_precedence(self):
        for changes,outcome in (({},'ready'),({'create_fingerprint':'b'*64},'conflict'),({'deleted_at':'clock','create_fingerprint':'b'*64},'gone')):
            row=object_row();row.update(changes);db=Database(args(),{'locked':None},{'object':row})
            self.assertEqual(create(db)['outcome'],outcome);self.assertEqual(len(db.tx.calls),3);self.assertEqual(db.tx.savepoints,[])

    def test_validation_before_lock_native_json_size_and_name_rules(self):
        for changes in ({'kind':None},{'id':None},{'name':''},{'name_clean':False},{'fingerprint':'A'*64},
                        {'channel_id':CHANNEL},{'conditions_sql_null':False}):
            db=Database(args(**changes))
            with self.assertRaises(DatabaseServerError) as caught:create(db)
            self.assertEqual(caught.exception.code,'22023');self.assertEqual(len(db.tx.calls),1)
            self.assertEqual(db.ends,['rollback'])
        self.assertIn('octet_length(conditions::text)',mutations._PREPARE_OBJECT)
        self.assertIn("name !~ '[[:cntrl:]]'",mutations._PREPARE_OBJECT)

    def test_view_final_visibility_fence_follows_insert_without_permission_rule(self):
        row=object_row();row.update(kind='view',channel_id=CHANNEL,conditions={})
        db=Database(args(kind='view',channel_id=CHANNEL,conditions={},conditions_sql_null=False,conditions_bytes=2),
            {'locked':None},None,{'control_epoch':7},{'object':row},{'is_hidden':False,'control_epoch':7})
        result=mutations.create_saved_object(db,kind='view',object_id=str(ID),name='Synthetic',fingerprint=FINGERPRINT,channel_id=CHANNEL,conditions={})
        self.assertEqual(result['outcome'],'ready');self.assertEqual(db.tx.calls[-2][0],'returning')
        self.assertIn('FOR SHARE',db.tx.calls[-1][1]);self.assertNotIn('owner_permission',db.tx.calls[-1][1])
        self.assertEqual(db.tx.savepoints,['begin','release'])

    def test_lost_view_scope_rolls_back_savepoint_but_unavailable_outcome_commits(self):
        for scope in (None,{'is_hidden':True,'control_epoch':7},{'is_hidden':False,'control_epoch':8}):
            db=Database(args(kind='view',channel_id=CHANNEL,conditions={},conditions_sql_null=False,conditions_bytes=2),
                {'locked':None},None,{'control_epoch':7},{'object':object_row()},scope)
            result=mutations.create_saved_object(db,kind='view',object_id=str(ID),name='Synthetic',fingerprint=FINGERPRINT,channel_id=CHANNEL,conditions={})
            self.assertEqual(result,{'outcome':'channel_unavailable'})
            self.assertEqual(db.tx.savepoints,['begin','rollback']);self.assertEqual(db.ends,['commit'])

    def test_unavailable_view_before_insert_and_acl_failure_after_insert(self):
        base=[args(kind='view',channel_id=CHANNEL,conditions={},conditions_sql_null=False,conditions_bytes=2),{'locked':None},None]
        db=Database(*base,None)
        invoke=lambda db:mutations.create_saved_object(db,kind='view',object_id=str(ID),name='Synthetic',fingerprint=FINGERPRINT,channel_id=CHANNEL,conditions={})
        self.assertEqual(invoke(db),{'outcome':'channel_unavailable'});self.assertEqual(len(db.tx.calls),4)
        error=DatabaseServerError('database_operation_failed',code='42501')
        db=Database(*base,{'control_epoch':7},{'object':object_row()},error)
        with self.assertRaises(DatabaseServerError) as caught:invoke(db)
        self.assertIs(caught.exception,error);self.assertEqual(db.tx.savepoints,['begin','rollback']);self.assertEqual(db.ends,['rollback'])

    def test_existing_transaction_is_borrowed_without_new_operation(self):
        tx=Transaction(args(),{'locked':None},{'object':object_row()})
        self.assertEqual(create(None,transaction=tx)['outcome'],'ready')
        self.assertEqual(len(tx.calls),3)


class RenameDeleteTests(unittest.TestCase):
    def test_same_name_rename_still_increments_revision_and_updates_clock(self):
        changed=object_row();changed['revision']=3
        db=Database(args(),{'object':object_row()},{'object':changed})
        result=mutations.rename_saved_object(db,kind='collection',object_id=str(ID),name='Synthetic',expected_revision=2)
        self.assertEqual(result['object']['revision'],3)
        self.assertIn('revision=revision+1',db.tx.calls[-1][1]);self.assertIn('clock_timestamp()',db.tx.calls[-1][1])

    def test_rename_missing_gone_and_cas_conflict_without_update(self):
        for row,outcome in ((None,'not_found'),({**object_row(),'deleted_at':'clock'},'gone'),({**object_row(),'revision':3},'conflict')):
            db=Database(args(),None if row is None else {'object':row})
            self.assertEqual(mutations.rename_saved_object(db,kind='collection',object_id=str(ID),name='Synthetic',expected_revision=2),{'outcome':outcome})
            self.assertEqual(len(db.tx.calls),2)

    def test_delete_clears_members_and_tombstones_without_removing_receipts(self):
        db=Database(args(),{'object':object_row()},1,1)
        result=mutations.delete_saved_object(db,kind='collection',object_id=str(ID),expected_revision=2)
        self.assertEqual(result,{'outcome':'deleted','id':str(ID)})
        self.assertIn('DELETE FROM public.saved_collection_members',db.tx.calls[-2][1])
        statement=db.tx.calls[-1][1];self.assertEqual(statement.count('clock_timestamp()'),2)
        self.assertIn('member_count=0',statement);self.assertNotIn('saved_collection_commands',' '.join(c[1] for c in db.tx.calls))

    def test_delete_replay_expected_old_revision_and_bigint_overflow(self):
        row={**object_row(),'deleted_at':'clock','revision':3}
        for revision,outcome in ((2,'deleted'),(3,'conflict')):
            db=Database(args(expected_revision=revision),{'object':row})
            self.assertEqual(mutations.delete_saved_object(db,kind='collection',object_id=str(ID),expected_revision=revision)['outcome'],outcome)
            self.assertEqual(len(db.tx.calls),2)
        db=Database(args(expected_revision=9223372036854775807),{'object':row})
        with self.assertRaises(DatabaseServerError) as caught:mutations.delete_saved_object(db,kind='collection',object_id=str(ID),expected_revision=9223372036854775807)
        self.assertEqual(caught.exception.code,'22003')


class MembershipTests(unittest.TestCase):
    def test_add_sorted_ids_shared_clock_count_revision_receipt_same_transaction(self):
        changed={**object_row(),'revision':3,'member_count':3}
        db=Database(member_args(video_ids=['bbbbbbbbbbb','aaaaaaaaaaa']),{'object':object_row()},None,
            {'value':NOW},2,{'object':changed})
        result=members(db,video_ids=['bbbbbbbbbbb','aaaaaaaaaaa'])
        self.assertEqual(result['mutation'],dict(request_id=str(REQUEST),replayed=False,requested_count=2,changed_count=2,applied_revision=3))
        self.assertIn('ORDER BY item',db.tx.calls[4][1]);self.assertIn('ON CONFLICT',db.tx.calls[4][1])
        self.assertEqual(db.tx.calls[4][2][1],db.tx.calls[5][2]['changed_at']);self.assertEqual(len(db.operations),1)
        self.assertEqual(len(db.tx.calls),6);self.assertEqual(db.tx.calls[5][0],'finalize')
        self.assertEqual(db.tx.calls[5][2]['changed_count'],2);self.assertEqual(db.tx.calls[5][2]['requested_count'],2)
        self.assertIn('INSERT INTO public.saved_collection_commands',db.tx.calls[5][1]);self.assertEqual(db.ends,['commit'])

    def test_remove_applies_negative_count(self):
        changed={**object_row(),'revision':3,'member_count':0}
        db=Database(member_args(action='remove'),{'object':object_row()},None,{'value':NOW},1,{'object':changed})
        result=members(db,action='remove')
        self.assertEqual(result['collection']['member_count'],0);self.assertEqual(db.tx.calls[5][2]['delta'],-1)
        self.assertIn('video_id=ANY',db.tx.calls[4][1])

    def test_noop_has_receipt_but_no_revision_or_clock_update(self):
        db=Database(member_args(),{'object':object_row()},None,{'value':NOW},0,1)
        result=members(db)
        self.assertEqual(result['mutation']['changed_count'],0);self.assertEqual(result['collection']['revision'],2)
        self.assertFalse(any(call[0]=='returning' for call in db.tx.calls))
        self.assertEqual(db.tx.calls[-1][2][-1],2)

    def test_receipt_replay_precedes_revision_and_returns_current_object(self):
        row={**object_row(),'revision':9}
        receipt=dict(request_fingerprint=FINGERPRINT,requested_count=4,changed_count=2,applied_revision=3)
        db=Database(member_args(expected_revision=1,action='remove'),{'object':row},receipt)
        result=members(db,expected_revision=1,action='remove')
        self.assertTrue(result['mutation']['replayed']);self.assertEqual(result['mutation']['applied_revision'],3)
        self.assertEqual(result['collection']['revision'],9);self.assertEqual(result['mutation']['requested_count'],4)
        self.assertEqual(len(db.tx.calls),3)

    def test_receipt_fingerprint_conflict_and_gone_before_receipt(self):
        db=Database(member_args(),{'object':object_row()},dict(request_fingerprint='b'*64))
        self.assertEqual(members(db),{'outcome':'conflict'})
        db=Database(member_args(),{'object':{**object_row(),'deleted_at':'clock'}})
        self.assertEqual(members(db),{'outcome':'gone'});self.assertEqual(len(db.tx.calls),2)

    def test_stale_revision_no_receipt_or_dml(self):
        db=Database(member_args(expected_revision=1),{'object':object_row()},None)
        self.assertEqual(members(db,expected_revision=1),{'outcome':'conflict'})
        self.assertEqual(len(db.tx.calls),3)

    def test_validation_cardinality_duplicates_request_uuid_and_fingerprint(self):
        for changes in ({'video_ids':None},{'video_ids':[]},{'video_ids':['aaaaaaaaaaa']*501},
                        {'video_ids':['aaaaaaaaaaa']*2},{'video_ids':[None]},{'video_ids':['invalid']},
                        {'request_id':UUID(int=0)},{'fingerprint':'b'},{'action':None},{'expected_revision':0}):
            db=Database(member_args(**changes))
            with self.assertRaises(DatabaseServerError) as caught:members(db)
            self.assertEqual(caught.exception.code,'22023');self.assertEqual(len(db.tx.calls),1)

    def test_receipt_failure_rolls_back_changes_and_is_never_retried(self):
        error=DatabaseServerError('database_operation_failed',code='23514')
        row={**object_row(),'revision':1,'member_count':0}
        db=Database(member_args(expected_revision=1,action='remove'),{'object':row},None,{'value':NOW},0,error)
        with self.assertRaises(DatabaseServerError) as caught:members(db,expected_revision=1,action='remove')
        self.assertIs(caught.exception,error);self.assertEqual(db.ends,['rollback']);self.assertEqual(len(db.operations),1)
        unknown=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        db=Database(member_args(),{'object':object_row()},None,{'value':NOW},unknown)
        with self.assertRaises(DatabaseUnavailable) as caught:members(db)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.tx.calls),5)



if __name__=='__main__':unittest.main()

