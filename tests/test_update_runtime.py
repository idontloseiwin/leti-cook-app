"""U4 machine proofs are finite, read-only, path/identity/time bound."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from hot_runtime import HotAdmissionConfig, ACCESS_REASONS, RemoteAdmission, signed_status
from self_hosted_auth_protocol import worker_proof
from update_runtime_protocol import (HEADER, STATUS_PATH, HOT_REASONS, UpdateRuntimeError,
    request_proof, valid_request_proof, sign_status, verify_status)
from update_runtime_status import UpdateRuntimeClient, signed_update_status
from test_runtime_gates import runtime
from ops.update_runtime import main

ADMISSION=HotAdmissionConfig('dddddddd-dddd-4ddd-8ddd-dddddddddddd','synthetic-update-status-test-secret-0000')
OP='a'*32
NOW=1800000000.0


def fixture():
    controller,clocks=runtime()
    controller.initialize()
    controller.update_hold_guard=SimpleNamespace(check=lambda:{'state':'held','operation_id':OP})
    manager=SimpleNamespace(lock=threading.RLock(),thread=None,
        collection=SimpleNamespace(update_status=lambda:{'observed_operation_id':OP,'active':False,'outcome':'idle'}))
    return controller,manager


def document():
    controller,manager=fixture()
    return signed_update_status(controller,manager,admission=ADMISSION,now=NOW)


def encoded(value):
    return json.dumps(value,allow_nan=True).encode()


class ProofTests(unittest.TestCase):
    def test_request_proof_cannot_be_replayed_as_hot_runtime_or_from_another_time(self):
        proof=request_proof(ADMISSION.secret,now=NOW)
        self.assertTrue(valid_request_proof(proof,ADMISSION.secret,now=NOW))
        self.assertFalse(valid_request_proof(worker_proof(ADMISSION.secret,now=NOW),ADMISSION.secret,now=NOW))
        self.assertFalse(valid_request_proof(proof,ADMISSION.secret,now=NOW+11))
        self.assertFalse(valid_request_proof(proof,ADMISSION.secret,now=NOW-2))

    def test_valid_status_has_no_signature_in_trusted_cli_document(self):
        value=verify_status(encoded(document()),identity=ADMISSION.identity,key=ADMISSION.secret,expected_operation=OP,now=NOW)
        self.assertNotIn('signature',value)
        self.assertEqual(value['collector']['outcome'],'idle')
        expected=json.loads((ROOT/'application/schema-release.json').read_text(encoding='utf-8'))
        self.assertEqual(value['schema'],{'baseline_id':expected['baseline_id'],'version':expected['schema_version'],'ready':True})
        self.assertEqual(HOT_REASONS,ACCESS_REASONS)

    def test_changed_identity_signature_deadline_or_expected_operation_is_rejected(self):
        good=document()
        for identity,key,now,op in [('0'*64,ADMISSION.secret,NOW,OP),
            (ADMISSION.identity,'wrong-key',NOW,OP),(ADMISSION.identity,ADMISSION.secret,NOW+5,OP),
            (ADMISSION.identity,ADMISSION.secret,NOW-2,OP),(ADMISSION.identity,ADMISSION.secret,NOW,'b'*32)]:
            with self.subTest(identity=identity,now=now,op=op),self.assertRaises(UpdateRuntimeError):
                verify_status(encoded(good),identity=identity,key=key,expected_operation=op,now=now)

    def test_even_signed_contradictory_or_extra_fields_are_refused(self):
        good=document();good.pop('signature')
        changes=[lambda v:v.update(extra=True),lambda v:v.update(expires_at=NOW+6),
            lambda v:v['collector'].update(active=True,outcome='normal'),
            lambda v:v['hold'].update(state='absent'),lambda v:v['hot']['collection'].update(allowed=True,reason='ready'),
            lambda v:v['schema'].update(ready=False),lambda v:v.update(setup_active=1),
            lambda v:v['hot']['user_work'].update(reason='new-unreviewed-reason')]
        for change in changes:
            bad=copy.deepcopy(good);change(bad)
            with self.subTest(value=bad),self.assertRaises(UpdateRuntimeError):
                verify_status(encoded(sign_status(bad,ADMISSION.secret)),identity=ADMISSION.identity,key=ADMISSION.secret,now=NOW)

    def test_duplicate_json_and_oversized_response_never_pass(self):
        raw=encoded(document())
        for bad in (b'{"version":1,'+raw[1:],raw+b' '*4096):
            with self.assertRaises(UpdateRuntimeError):
                verify_status(bad,identity=ADMISSION.identity,key=ADMISSION.secret,now=NOW)

    def test_schema_is_not_claimed_on_unverified_or_failed_database(self):
        controller,manager=fixture()
        controller._database_reason='schema_invalid'
        value=signed_update_status(controller,manager,admission=ADMISSION,now=NOW)
        self.assertEqual(value['schema'],{'baseline_id':None,'version':None,'ready':False})
        self.assertFalse(value['hot']['user_work']['allowed'])

    def test_epoch_tracks_process_not_cleanup_reset_and_setup_state_is_real(self):
        controller,manager=fixture()
        epoch=controller.update_epoch
        controller._reset_after_pause()
        self.assertEqual(controller.update_epoch,epoch)
        other,_=fixture()
        self.assertNotEqual(other.update_epoch,epoch)
        manager.thread=SimpleNamespace(is_alive=lambda:True)
        self.assertTrue(signed_update_status(controller,manager,admission=ADMISSION,now=NOW)['setup_active'])

    def test_existing_worker_proof_accepts_new_hold_reason_but_collection_is_blocked(self):
        controller,_=fixture()
        response=signed_status(controller,admission=ADMISSION,now=NOW)
        remote=RemoteAdmission('http://127.0.0.1:8002/internal/hot-runtime',admission=ADMISSION,
            fetch=lambda:response,clock=lambda:NOW,monotonic=lambda:0)
        remote.require_database_ready()
        remote.require_api_ready()
        from hot_runtime import HotRuntimeUnavailable
        with self.assertRaises(HotRuntimeUnavailable):remote.require_collection_ready()

    def test_client_derives_exact_loopback_route_and_refuses_other_origins(self):
        client=UpdateRuntimeClient('http://127.0.0.1:8002/internal/hot-runtime',admission=ADMISSION,
            fetch=lambda:encoded(document()),clock=lambda:NOW,monotonic=lambda:0)
        self.assertEqual(client.url,'http://127.0.0.1:8002/internal/update-status')
        self.assertEqual(client.status(expected_operation=OP)['hold']['operation_id'],OP)
        for url in ('https://127.0.0.1:8002/internal/hot-runtime','http://example.com:8002/internal/hot-runtime',
                    'http://user:pw@127.0.0.1:8002/internal/hot-runtime','http://127.0.0.1:8002/internal/update-status',
                    'http://127.0.0.1:8002/internal/hot-runtime?secret=x'):
            with self.assertRaises(UpdateRuntimeError):UpdateRuntimeClient(url,admission=ADMISSION)

    def test_cli_is_readonly_supports_preflight_and_redacts_errors(self):
        env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres',
             'LIC_DATABASE_ID':ADMISSION.database_id,'LIC_HOT_RUNTIME_SECRET':ADMISSION.secret,
             'LIC_HOT_RUNTIME_URL':'http://127.0.0.1:8002/internal/hot-runtime'}
        for args in (['status'],['status','--operation',OP]):
            output=io.StringIO()
            with patch.dict(os.environ,env),patch('update_runtime_status.UpdateRuntimeClient.status',return_value={'verified':True}) as status,redirect_stdout(output):
                self.assertEqual(main(args),0)
                status.assert_called_once_with(expected_operation=OP if len(args)>1 else None)
            self.assertEqual(json.loads(output.getvalue()),{'verified':True})
        error=io.StringIO();output=io.StringIO()
        with patch.dict(os.environ,env),patch('update_runtime_status.UpdateRuntimeClient.status',side_effect=ValueError('secret-provider-data')),redirect_stderr(error),redirect_stdout(output):
            self.assertEqual(main(['status']),1)
        self.assertEqual(output.getvalue(),'')
        self.assertEqual(json.loads(error.getvalue()),{'code':'update_status_unavailable'})


class StatusHttpTests(unittest.TestCase):
    def client(self,*,peer='127.0.0.1'):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.middleware import install_runtime_middleware
        from self_hosted_auth import SelfHostedAuthMiddleware
        from self_hosted_onboarding import SetupMiddleware
        controller,manager=fixture()
        manager.ready=Mock(side_effect=AssertionError('browser setup called'))
        app=FastAPI()
        app.state.hot_runtime=controller
        app.state.setup_manager=manager
        with patch('web.middleware.selected_deployment_profile',return_value='self-hosted'):
            install_runtime_middleware(app,core=SimpleNamespace(HOT_ADMISSION_CONFIG=ADMISSION))
        app.add_middleware(SetupMiddleware,manager=manager)
        app.add_middleware(SelfHostedAuthMiddleware,repository=SimpleNamespace(configured=lambda:True),
            origin='http://127.0.0.1:8002',worker_key=ADMISSION.secret)
        return TestClient(app,base_url='http://127.0.0.1:8002',client=(peer,12345))

    def test_actual_endpoint_needs_no_browser_but_requires_correct_machine_proof(self):
        with self.client() as client:
            self.assertEqual(client.get(STATUS_PATH).status_code,403)
            self.assertEqual(client.get(STATUS_PATH,headers={HEADER:worker_proof(ADMISSION.secret)}).status_code,403)
            response=client.get(STATUS_PATH,headers={HEADER:request_proof(ADMISSION.secret)})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.headers['cache-control'],'no-store')
            verified=verify_status(response.content,identity=ADMISSION.identity,key=ADMISSION.secret,expected_operation=OP)
            self.assertFalse(verified['setup_active'])
            self.assertNotIn('source_expires_at',verified)
            self.assertEqual(client.post(STATUS_PATH,headers={HEADER:request_proof(ADMISSION.secret)}).status_code,403)

    def test_remote_peer_is_refused_even_with_valid_signature(self):
        with self.client(peer='192.0.2.10') as client:
            self.assertEqual(client.get(STATUS_PATH,headers={HEADER:request_proof(ADMISSION.secret)}).status_code,403)


if __name__=='__main__':unittest.main()
