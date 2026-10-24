"""Offline tests for prepared atomic/bounded user-work import."""
from contextlib import contextmanager
from collections import deque
from uuid import UUID
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.user_work import user_work_restore as restore
from db.base.postgres import DatabaseServerError

STAMP='2026-01-01T00:00:00Z'
IDENTITY='a0000000-0000-4000-8000-000000000001'
OTHER='a0000000-0000-4000-8000-000000000002'
COLLECTION='c0000000-0000-4000-8000-000000000001'
VIEW='e0000000-0000-4000-8000-000000000001'


def document():return dict(schema_version=2,channels=[],collections=[],folders=[],labels=[],reviews=[])


def channel(**updates):
    item=dict(id=IDENTITY,user_input=None,folder_id=None,parent_ref=None,is_hidden=False,added_at=STAMP,label_keys=[],saved_views=[])
    item.update(updates);return item


def saved_view(**updates):
    item=dict(id=VIEW,name='View',revision=2,created_at=STAMP,updated_at=STAMP,create_fingerprint='a'*64,
        conditions=dict(version=1,filters={},facets={}))
    item.update(updates);return item


def collection(members=()):
    return dict(id=COLLECTION,name='Collection',revision=2,created_at=STAMP,updated_at=STAMP,create_fingerprint='b'*64,members=list(members))


class Transaction:
    def __init__(self):
        self.calls=[];self.savepoints=[];self.empty=True;self.tombs={};self.pending=[];self.fail={}
        self.cast_errors={};self.fact_overrides={};self.bad_reference=False;self.missing_parent=False;self.cycle=False;self.duplicates=False
    def _record(self,mode,statement,params=()):
        self.calls.append((mode,statement,tuple(params)))
        for fragment,error in self.fail.items():
            if fragment in statement:raise error
    def fetch_one(self,statement,params=()):
        self._record('read',statement,params)
        if 'jsonb_typeof(%s::jsonb)' in statement:
            value=params[0].obj if params[0] is not None else None
            return {'kind':'object' if isinstance(value,dict) else 'null','bytes':100}
        if statement==restore._EMPTY:return {'empty':self.empty}
        if 'jsonb_object_agg' in statement:return {'value':self.tombs}
        if "label_key='monetized'" in statement:return {'present':True}
        if ' AS missing' in statement:return {'missing':self.missing_parent}
        if 'WITH RECURSIVE tree' in statement:return {'value':0 if self.cycle else len(self.pending)}
        if 'HAVING count(*)>1' in statement:return {'value':self.duplicates}
        raise AssertionError('unexpected prepared SELECT')
    def fetch_all(self,statement,params=(),*,max_rows):
        self._record('reads',statement,params)
        values=params[0].obj
        if statement==restore._NATIVE_FACTS:
            result=[]
            for i,item in enumerate(values,1):
                value=item['value'];key=str(value)
                row=dict(position=i,error_code=self.cast_errors.get(key),number=value if type(value)is int else None,
                    finite=value is not None,not_future=True,no_controls=not(isinstance(value,str) and '\n' in value),
                    trimmed=not(isinstance(value,str) and value!=value.strip(' ')),json_bytes=2)
                row.update(self.fact_overrides.get(key,{}));result.append(row)
            return result
        return [{'ord':i,'valid':not self.bad_reference} for i,_ in enumerate(values,1)]
    def execute(self,statement,params=()):
        self._record('write',statement,params)
        if statement==restore._PENDING:self.pending.extend(params[-1].obj)
        return 1
    def lock_user_work_import(self):self._record('lock','fixed_user_work_import_lock')
    @contextmanager
    def borrow(self):
        self.calls.append(('borrow','begin',()))
        try:yield self
        except BaseException:self.calls.append(('borrow','failed',()));raise

    @contextmanager
    def savepoint(self):
        before=deepcopy(self.pending);self.savepoints.append('begin')
        try:yield self
        except BaseException:self.pending=before;self.savepoints.append('rollback');raise
        else:self.savepoints.append('release')


class Database:
    def __init__(self):self.tx=Transaction();self.operations=[];self.ends=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self.tx
        except BaseException:self.ends.append('rollback');raise
        else:self.ends.append('commit')


class ImportTests(unittest.TestCase):
    def test_empty_import_lock_order_and_one_total_budget(self):
        db=Database();result=restore.import_user_work_backup(db,document())
        self.assertEqual(result,dict(outcome='ready',pending_channels=0,saved_objects=0,members=0))
        self.assertEqual(len(db.operations),1);self.assertEqual(db.operations[0].total_timeout_ms,60000)
        self.assertEqual(db.tx.savepoints,['begin','release']);self.assertEqual(db.ends,['commit'])
        modes=[row[0] for row in db.tx.calls];self.assertLess(modes.index('lock'),modes.index('write'))
        self.assertEqual(sum(mode=='lock' for mode in modes),1)

    def test_workspace_nonempty_does_not_mutate_after_locked_recheck(self):
        db=Database();db.tx.empty=False
        self.assertEqual(restore.import_user_work_backup(db,document()),{'outcome':'workspace_not_empty'})
        self.assertFalse(any(row[0]=='write' for row in db.tx.calls));self.assertIn('lock',[row[0] for row in db.tx.calls])

    def test_nowait_from_lock_or_any_later_step_rolls_back_then_returns_busy(self):
        for where in ('fixed_user_work_import_lock','DELETE FROM public.saved_objects'):
            db=Database();db.tx.fail[where]=DatabaseServerError('database_operation_failed',code='55P03')
            self.assertEqual(restore.import_user_work_backup(db,document()),{'outcome':'busy'})
            self.assertEqual(db.tx.savepoints,['begin','rollback']);self.assertEqual(db.ends,['commit'])

    def test_non_lock_error_propagates_and_is_not_retried(self):
        db=Database();error=DatabaseServerError('database_operation_failed',code='42501');db.tx.fail['fixed_user_work_import_lock']=error
        with self.assertRaises(DatabaseServerError) as caught:restore.import_user_work_backup(db,document())
        self.assertIs(caught.exception,error);self.assertEqual(db.ends,['rollback']);self.assertEqual(len(db.operations),1)

    def test_envelope_versions_types_counts_and_channel_shapes_before_locks(self):
        cases=[None,[],{**document(),'schema_version':True},{**document(),'unknown':0},
            {**document(),'folders':{}},{**document(),'channels':[channel(is_hidden=1)]},
            {**document(),'collections':[{'members':None}]},{**document(),'reviews':[{'history':None}]},
            {**document(),'folders':[{}]*1001}]
        for value in cases:
            db=Database()
            with self.subTest(value_type=type(value)),self.assertRaises(DatabaseServerError):restore.import_user_work_backup(db,value)
            self.assertFalse(any(row[0]=='lock' for row in db.tx.calls))
        db=Database();self.assertEqual(restore.import_user_work_backup(db,{**document(),'schema_version':2.0})['outcome'],'ready')

    def test_original_hidden_clock_validation_uses_native_cast_error_or_finite_future_flag(self):
        for value,error,overrides,expected in [('invalid','22007',{},'22007'),('infinity',None,{'finite':False},'P0001'),
            ('future',None,{'not_future':False},'P0001')]:
            db=Database();db.tx.cast_errors[value]=error;db.tx.fact_overrides[value]=overrides
            with self.assertRaises(DatabaseServerError) as caught:
                restore.import_user_work_backup(db,{**document(),'channels':[channel(is_hidden=True,hidden_at=value)]})
            self.assertEqual(caught.exception.code,expected)
            self.assertFalse(any(row[0]=='lock' for row in db.tx.calls))

    def test_future_clock_is_requested_only_for_initial_hidden_validation(self):
        db=Database()
        value={**document(),'channels':[channel(is_hidden=True,hidden_at=STAMP,saved_views=[saved_view()])]}
        restore.import_user_work_backup(db,value)
        calls=[(i,params[0].obj) for i,(_,sql,params) in enumerate(db.tx.calls) if sql==restore._NATIVE_FACTS]
        hidden=[(i,item) for i,rows in calls for item in rows if item['check_future']]
        self.assertEqual(len(hidden),1)
        lock=next(i for i,row in enumerate(db.tx.calls) if row[0]=='lock')
        self.assertLess(hidden[0][0],lock)
        self.assertEqual(hidden[0][1]['value'],STAMP)
        self.assertEqual(sum(item['kind']=='timestamptz' and not item['check_future'] for _,rows in calls for item in rows),2)
        self.assertIn("kind='timestamptz' AND check_future",restore._NATIVE_FACTS)

    def test_pending_views_and_tombstone_revision_floor_remain_native(self):
        db=Database();db.tx.tombs={'view:'+VIEW:7}
        result=restore.import_user_work_backup(db,{**document(),'channels':[channel(saved_views=[saved_view()])]})
        self.assertEqual(result['saved_objects'],1);self.assertEqual(result['pending_channels'],1)
        calls=[row for row in db.tx.calls if row[1]==restore._PENDING]
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][2][0].obj,db.tx.tombs)
        self.assertIn('greatest',restore._PENDING);self.assertIn('::bigint+1',restore._PENDING)
        self.assertIn('ORDER BY vo',restore._PENDING)

    def test_channel_batching_is_bounded_not_per_channel(self):
        channels=[channel(id=f'a0000000-0000-4000-8000-{i:012d}') for i in range(1001)]
        db=Database();restore.import_user_work_backup(db,{**document(),'channels':channels})
        batches=[row[2][-1].obj for row in db.tx.calls if row[1]==restore._PENDING]
        self.assertEqual([len(batch) for batch in batches],[500,500,1])

    def test_member_batching_and_pending_owner_are_preserved(self):
        members=[dict(video_id=f'{i:011d}',added_at=STAMP,channel_ref=IDENTITY) for i in range(1001)]
        db=Database();result=restore.import_user_work_backup(db,{**document(),'channels':[channel()],'collections':[collection(members)]})
        self.assertEqual(result['members'],1001)
        batches=[row[2][-1].obj for row in db.tx.calls if row[1]==restore._MEMBERS]
        self.assertEqual([len(batch) for batch in batches],[500,500,1]);self.assertIn('restore_owner_ref',restore._MEMBERS)

    def test_earlier_constraint_failure_precedes_later_folder_domain_error(self):
        value={**document(),'folders':[dict(id='bad-native-id',name='Valid',created_at=STAMP),dict(id=OTHER,name='',created_at=STAMP)]}
        db=Database();error=DatabaseServerError('database_operation_failed',code='22P02');db.tx.fail['INSERT INTO public.folders']=error
        with self.assertRaises(DatabaseServerError) as caught:restore.import_user_work_backup(db,value)
        self.assertIs(caught.exception,error)
        write=next(row for row in db.tx.calls if row[1]==restore._FOLDERS)
        self.assertEqual(len(write[2][0].obj),1)

    def test_later_folder_domain_failure_flushes_prefix_then_rolls_back(self):
        value={**document(),'folders':[dict(id=IDENTITY,name='Valid',created_at=STAMP),dict(id=OTHER,name='',created_at=STAMP)]}
        db=Database()
        with self.assertRaises(DatabaseServerError) as caught:restore.import_user_work_backup(db,value)
        self.assertEqual(caught.exception.message,'user_work_folder_invalid')
        self.assertEqual(db.tx.savepoints,['begin','rollback']);self.assertEqual(db.ends,['rollback'])
        self.assertEqual(len(next(row for row in db.tx.calls if row[1]==restore._FOLDERS)[2][0].obj),1)

    def test_label_reference_parent_missing_cycle_and_duplicate_view_errors(self):
        for field,message in [('bad_reference','user_work_label_reference'),('missing_parent','user_work_parent_missing'),
                              ('cycle','user_work_parent_cycle'),('duplicates','user_work_duplicate_identity')]:
            db=Database();setattr(db.tx,field,True)
            with self.assertRaises(DatabaseServerError) as caught:restore.import_user_work_backup(db,{**document(),'channels':[channel()]})
            self.assertEqual(caught.exception.message,message);self.assertEqual(db.ends,['rollback'])

    def test_saved_view_bool_version_and_numeric_fraction_native_revision(self):
        db=Database()
        with self.assertRaises(DatabaseServerError) as caught:
            restore.import_user_work_backup(db,{**document(),'channels':[channel(saved_views=[saved_view(conditions={'version':True,'filters':{},'facets':{}})])]})
        self.assertEqual(caught.exception.message,'user_work_saved_view_invalid')
        db=Database();db.tx.cast_errors['1.0']='22P02'
        with self.assertRaises(DatabaseServerError) as caught:
            restore.import_user_work_backup(db,{**document(),'channels':[channel(saved_views=[saved_view(revision=1.0)])]})
        self.assertEqual(caught.exception.code,'22P02')

    def test_borrowed_transaction_does_not_open_independent_scope(self):
        tx=Transaction();result=restore.import_user_work_backup(None,document(),transaction=tx)
        self.assertEqual(result['outcome'],'ready');self.assertEqual(tx.savepoints,['begin','release'])
        self.assertIn(('borrow','begin',()),tx.calls)

    def test_native_fact_query_never_calls_retained_business_functions(self):
        self.assertIn('pg_input_error_info',restore._NATIVE_FACTS);self.assertIn('clock_timestamp()',restore._NATIVE_FACTS)
        self.assertNotIn('public.',restore._NATIVE_FACTS)
        facts=restore._Facts()
        with self.assertRaises(ValueError):facts.add(('bad',),'arbitrary_type','value')



class PendingTransaction(Transaction):
    def __init__(self,*results):super().__init__();self.results=deque(results)
    def read(self,mode,statement,params=()):
        self._record(mode,statement,params);result=self.results.popleft()
        if isinstance(result,Exception):raise result
        return result
    def fetch_one(self,statement,params=()):return self.read('read',statement,params)
    def returning_one(self,statement,params=()):return self.read('returning',statement,params)


class PendingUpdateTests(unittest.TestCase):
    def invoke(self,*results,**values):
        self.db=Database();self.db.tx=PendingTransaction(*results)
        return restore.update_user_channel_restore_item(self.db,identity=IDENTITY,expected_revision=1,user_input=None,parent_ref=None,**values)
    def args(self,**updates):
        value=dict(id=UUID(IDENTITY),revision=1,user_input=None,parent_ref=None);value.update(updates);return value
    def row(self,**updates):
        value=dict(id=UUID(IDENTITY),revision=1,user_input=None,parent_ref=None);value.update(updates);return value

    def test_unchanged_input_still_increments_revision_and_returns_exact_shape(self):
        item=dict(id=IDENTITY,revision=2,user_input=None,parent_ref=None)
        result=self.invoke(self.args(),{'locked':True},self.row(),{'pending':False,'tracked':False},{'value':item})
        self.assertEqual(result,{'outcome':'ready','item':item})
        self.assertIn('815152610,1',self.db.tx.calls[1][1]);self.assertIn('FOR UPDATE NOWAIT',self.db.tx.calls[2][1])
        self.assertIn('revision=revision+1',self.db.tx.calls[-1][1]);self.assertEqual(self.db.ends,['commit'])

    def test_busy_missing_and_revision_conflict_avoid_mutation(self):
        vectors=[([self.args(),{'locked':False}],'busy'),([self.args(),{'locked':True},None],'not_found'),
                 ([self.args(),{'locked':True},self.row(revision=2)],'conflict')]
        for rows,outcome in vectors:
            self.assertEqual(self.invoke(*rows),{'outcome':outcome})
            self.assertFalse(any(row[0]=='returning' for row in self.db.tx.calls))

    def test_nonpositive_or_maximum_revision_rejected_before_advisory_lock(self):
        for revision in (None,0,9007199254740991):
            with self.assertRaises(DatabaseServerError) as caught:self.invoke(self.args(revision=revision))
            self.assertEqual(caught.exception.message,'user_work_pending_request_invalid');self.assertEqual(len(self.db.tx.calls),1)

    def test_identity_collisions_and_parent_graph_outcomes(self):
        base=[self.args(user_input='https://www.youtube.com/@new'),{'locked':True},self.row()]
        self.assertEqual(self.invoke(*base,{'pending':True,'tracked':False}),{'outcome':'conflict'})
        self.assertEqual(self.invoke(*base,{'pending':False,'tracked':True}),{'outcome':'conflict'})
        self.assertEqual(self.invoke(self.args(parent_ref=UUID(IDENTITY)),{'locked':True},self.row(),{'pending':False,'tracked':False}),{'outcome':'conflict'})
        base=[self.args(parent_ref=UUID(OTHER)),{'locked':True},self.row(),{'pending':False,'tracked':False}]
        self.assertEqual(self.invoke(*base,{'present':False}),{'outcome':'parent_missing'})
        self.assertEqual(self.invoke(*base,{'present':True},{'cycle':True}),{'outcome':'conflict'})
        self.assertIn('UNION SELECT r.parent_ref',self.db.tx.calls[-1][1])

    def test_nowait_and_late_statement_failure_roll_back_savepoint(self):
        error=DatabaseServerError('database_operation_failed',code='55P03')
        self.assertEqual(self.invoke(self.args(),{'locked':True},error),{'outcome':'busy'})
        self.assertEqual(self.db.tx.savepoints,['begin','rollback']);self.assertEqual(self.db.ends,['commit'])
        error=DatabaseServerError('database_operation_failed',code='23514')
        with self.assertRaises(DatabaseServerError):self.invoke(self.args(),{'locked':True},self.row(),{'pending':False,'tracked':False},error)
        self.assertEqual(self.db.tx.savepoints,['begin','rollback']);self.assertEqual(self.db.ends,['rollback'])


if __name__=='__main__':unittest.main()

