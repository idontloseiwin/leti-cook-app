"""F4 install/update admission state and HTTP sharing; offline local SQLite only."""
from contextlib import closing, redirect_stdout, redirect_stderr
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'application'))
from db.user_work.reconnect_admission import ChannelAddAdmission, ChannelAddAdmissionUnavailable, configured_channel_add_admission
from ops import self_hosted_owner
from ops.update_host import Host, UpdateError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from http_test_support import channel_http, endpoint
from self_hosted_configuration import STARTER_CHANNEL_URLS
from web.models import ChannelRequest
from test_reconnect_admission import Clock, INSTALLATION


class AdmissionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.folder.chmod(0o700)
        self.auth=self.folder/'owner.sqlite3';self.path=self.folder/'channel-add.sqlite3'
        self.env=patch.dict(os.environ,{'LIC_AUTH_DATABASE':str(self.auth),'LIC_DATABASE_ID':INSTALLATION})
        self.env.start();self.addCleanup(self.env.stop)

    def test_fresh_explicit_owner_init_creates_private_sibling_and_never_resets(self):
        with patch.object(self_hosted_owner,'PASSWORD_HASHER',SimpleNamespace(hash=lambda value:'synthetic')):
            owner=self_hosted_owner.initialize(self.auth)
            self.assertFalse(owner.configured())
            configured_channel_add_admission().verify()
            self.assertEqual(configured_channel_add_admission().acquire(),0)
            before=self.path.read_bytes()
            with self.assertRaises(FileExistsError):self_hosted_owner.initialize(self.auth)
            self.assertEqual(self.path.read_bytes(),before)
        if os.name!='nt':self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_legacy_transition_creates_once_verify_current_preserves_all_events_and_clock(self):
        self_hosted_owner.prepare_channel_add_state(self.auth,legacy_transition=True)
        first=configured_channel_add_admission();first.acquire()
        before=self.path.read_bytes()
        for legacy in (False,True):
            self_hosted_owner.prepare_channel_add_state(self.auth,legacy_transition=legacy)
            self.assertEqual(self.path.read_bytes(),before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM channel_add_admissions').fetchone()[0],1)

    def test_current_missing_or_partial_or_foreign_store_is_never_recreated(self):
        with self.assertRaises(ChannelAddAdmissionUnavailable):self_hosted_owner.prepare_channel_add_state(self.auth)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b'partial');before=self.path.read_bytes()
        with self.assertRaises(ChannelAddAdmissionUnavailable):self_hosted_owner.prepare_channel_add_state(self.auth,legacy_transition=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.path.unlink()
        ChannelAddAdmission.initialize(self.path,INSTALLATION)
        with patch.dict(os.environ,{'LIC_DATABASE_ID':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}):
            with self.assertRaises(ChannelAddAdmissionUnavailable):self_hosted_owner.prepare_channel_add_state(self.auth,legacy_transition=True)

    def test_verify_does_not_admit_prune_or_advance_clock_and_rejects_invalid_rows(self):
        clock=Clock();owner=ChannelAddAdmission.initialize(self.path,INSTALLATION,clock=clock)
        owner.acquire();before=self.path.read_bytes();clock.now+=864000
        owner.verify();self.assertEqual(self.path.read_bytes(),before)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE channel_add_admissions SET admitted_us=admitted_us+1');db.commit()
        with self.assertRaises(ChannelAddAdmissionUnavailable):owner.verify()

    def test_shared_factory_requires_existing_absolute_auth_path_and_canonical_identity(self):
        for env in ({'LIC_AUTH_DATABASE':''},{'LIC_AUTH_DATABASE':'relative.sqlite3'},{'LIC_DATABASE_ID':'invalid'}):
            with patch.dict(os.environ,env),self.assertRaises(ChannelAddAdmissionUnavailable):configured_channel_add_admission()
        self.assertFalse(self.path.exists())

    def test_owner_command_emits_fixed_status_and_current_missing_refuses(self):
        for flag,code in (([],1),(['--legacy-transition'],0),([],0)):
            out=io.StringIO();err=io.StringIO()
            with patch.object(sys,'argv',['owner','prepare-channel-add-state',*flag]),redirect_stdout(out),redirect_stderr(err):
                self.assertEqual(self_hosted_owner.main(),code)
            self.assertNotIn(INSTALLATION,out.getvalue()+err.getvalue())
            self.assertNotIn(str(self.path),out.getvalue()+err.getvalue())
            if code==0:self.assertEqual(json.loads(out.getvalue()),{'status':'channel_add_state_ready'})


class OrdinaryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'channel-add.sqlite3';self.path.parent.chmod(0o700)
        self.clock=Clock();self.owner=ChannelAddAdmission.initialize(self.path,INSTALLATION,clock=self.clock)

    def client(self, owner):
        app=FastAPI();app.include_router(channel_http(channel_add_admission=owner).add_router)
        client=TestClient(app);self.addCleanup(client.close);return client

    def test_ordinary_and_separate_manual_instance_share_window_after_restart(self):
        manual=ChannelAddAdmission(self.path,INSTALLATION,clock=self.clock)
        for _ in range(19):self.assertEqual(manual.acquire(),0)
        with patch('web.channels.channel_service.add_channel',return_value={'status':'synthetic'}) as provider:
            client=self.client(self.owner)
            self.assertEqual(client.post('/add-channel',json={'url':'@synthetic'}).status_code,200)
            restarted=self.client(ChannelAddAdmission(self.path,INSTALLATION,clock=self.clock))
            denied=restarted.post('/add-channel',json={'url':'@synthetic'})
            self.assertEqual(denied.status_code,429);self.assertEqual(denied.headers['retry-after'],'60')
            self.assertEqual(denied.json()['detail']['code'],'channel_add_rate_limited')
            self.assertEqual(manual.acquire(),60);provider.assert_called_once()

    def test_missing_and_busy_state_fail_503_before_provider_and_no_memory_fallback(self):
        for missing in (False,True):
            lock=sqlite3.connect(self.path,isolation_level=None)
            try:
                if missing:lock.close();self.path.unlink()
                else:lock.execute('BEGIN IMMEDIATE')
                with patch('web.channels.channel_service.add_channel') as provider:
                    response=self.client(self.owner).post('/add-channel',json={'url':'@synthetic'})
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json()['detail']['code'],'channel_add_admission_unavailable')
                    self.assertNotIn('retry-after',response.headers);provider.assert_not_called()
                if missing:self.assertFalse(self.path.exists())
            finally:lock.close()

    def test_starter_bypass_does_not_consume_persistent_admission(self):
        denied=Mock();denied.acquire.side_effect=AssertionError('starter must retain its existing bypass')
        manager=Mock();http=channel_http(channel_add_admission=denied)
        with patch('self_hosted_onboarding.get_manager',return_value=manager),patch('web.channels.channel_service.add_channel',return_value={'status':'synthetic'}):
            asyncio.run(endpoint(http.add_router,'add_starter_channel')(ChannelRequest(url=next(iter(STARTER_CHANNEL_URLS))),None))
        denied.acquire.assert_not_called();manager.begin_starter.assert_called_once();manager.record_starter.assert_called_once()


class UpdateAdmissionTests(unittest.TestCase):
    def setUp(self):
        project='leti-cook-0123456789ab'
        self.layout=SimpleNamespace(installation=Path('/opt/leti-cook'),root=Path('/opt/leti-cook.update'),project=project)
        self.host=Host(self.layout)
        self.value={'phase':'new_start_intent','operation_id':'a'*32,'old_tree':{'device':1,'inode':1},'new_tree':{'device':1,'inode':2},
            'new_images':{'backend':'sha256:'+'b'*64},'old_images':{'backend':'sha256:'+'c'*64}}
        self.name=project+'_application-state'
        self.host.stop_helpers=Mock()
        self.host.inspect=Mock(return_value={'Name':self.name,'Driver':'local','Options':None,
            'Labels':{'com.docker.compose.project':project,'com.docker.compose.volume':'application-state'}})
        self.host.containers=Mock(return_value={'backend':{'Id':'d'*64,'State':{'Running':False},
            'Mounts':[{'Destination':'/var/lib/leti-cook','Type':'volume','Name':self.name}]}})
        self.host.docker=Mock(side_effect=['d'*64,''])
        self.host.helper=Mock(return_value='{"status":"channel_add_state_ready"}')
        self.host.compose=Mock()

    def run_prepare(self, flags, *, new=True):
        with patch('ops.update_host.journal.tree_identity',return_value=self.value['new_tree' if new else 'old_tree']),\
             patch('ops.update_host.journal.tree_position',return_value='new'),\
             patch('ops.update_candidate.channel_add_owner_present',side_effect=flags) as proof:
            self.host.start_backend(self.value,new=new)
            return proof

    def test_first_transition_uses_stopped_owned_volume_pinned_image_and_no_network_unprivileged_helper(self):
        proof=self.run_prepare([True,False])
        self.assertEqual([call.args[0] for call in proof.call_args_list],[self.layout.installation,self.layout.root/'candidate'])
        args=self.host.helper.call_args
        self.assertEqual(args.args[1],self.value['new_images']['backend'])
        self.assertEqual(args.args[2],['-B','ops/self_hosted_owner.py','prepare-channel-add-state','--legacy-transition'])
        self.assertEqual(args.kwargs['user'],'10001:10001');self.assertEqual(args.kwargs['capabilities'],())
        self.assertEqual(args.kwargs['timeout'],15)
        self.assertEqual(args.kwargs.get('network','none'),'none')
        self.assertEqual(args.kwargs['mounts'],['type=volume,source='+self.name+',target=/var/lib/leti-cook,volume-nocopy'])
        self.host.compose.assert_called_once()

    def test_current_owner_and_failed_update_recovery_verify_only(self):
        self.run_prepare([True,True])
        self.assertNotIn('--legacy-transition',self.host.helper.call_args.args[2])
        self.host.docker.reset_mock(side_effect=True);self.host.docker.side_effect=['d'*64,'']
        self.run_prepare([True],new=False)
        self.assertEqual(self.host.helper.call_args.args[1],self.value['old_images']['backend'])
        self.assertNotIn('--legacy-transition',self.host.helper.call_args.args[2])

    def test_legacy_recovery_leaves_additive_file_untouched(self):
        self.run_prepare([False],new=False)
        self.host.helper.assert_not_called();self.host.inspect.assert_not_called();self.host.compose.assert_called_once()

    def test_foreign_shared_running_or_unconfirmed_state_never_starts_backend(self):
        for fault in ('foreign','shared','running','unconfirmed','missing'):
            with self.subTest(fault=fault):
                self.setUp()
                if fault=='foreign':self.host.inspect.return_value['Labels']={}
                if fault=='shared':self.host.docker.side_effect=['foreign','']
                if fault=='running':self.host.containers.return_value['backend']['State']['Running']=True
                if fault=='unconfirmed':self.host.helper.return_value='{}'
                if fault=='missing':self.host.helper.side_effect=UpdateError('synthetic_helper_refused')
                with self.assertRaises(UpdateError):self.run_prepare([True,True])
                self.host.compose.assert_not_called()

    def test_tree_mismatch_or_no_journal_intent_refuses_before_helper(self):
        self.value['phase']='swapped'
        with self.assertRaises(UpdateError):self.run_prepare([True,False])
        self.host.helper.assert_not_called();self.host.compose.assert_not_called()


if __name__=='__main__':unittest.main()
