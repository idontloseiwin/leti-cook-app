"""Execute the real Bash resource owner against a file-backed fake Docker CLI."""
from pathlib import Path
import json,os,shutil,subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
FAKE=r"""
import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_STATE']);s=json.loads(p.read_text());a=sys.argv[1:];mode=os.environ['FAKE_SCENARIO'];cid='c'*64;image='sha256:'+'1'*64
s['calls'].append(a)
def finish(text='',code=0):
 p.write_text(json.dumps(s));print(text);raise SystemExit(code)
def value(flag):return a[a.index(flag)+1]
if a[:2]==['image','inspect']:finish(image)
if a[:2]==['container','ls']:
 if mode=='list_failure':finish(code=1)
 f=value('--filter')
 if mode=='collision' and not s['created']:finish('f'*64)
 c=s.get('container');finish(cid if c and (f=='id='+cid or f=='name=^/'+c['name']+'$') else '')
if a[0]=='create':
 s['created']+=1;name=value('--name');labels=[a[i+1] for i,x in enumerate(a) if x=='--label'];token=labels[0].split('=',1)[1];component=labels[1].split('=',1)[1]
 c={'id':cid,'name':name,'image':image,'token':token,'component':component};s['container']=c;s['volumes']=['a'*64]
 if mode=='wrong_name':c['name']='foreign-resource'
 if mode=='wrong_image':c['image']='sha256:'+'2'*64
 if mode=='wrong_token':c['token']='0'*32
 if mode=='wrong_component':c['component']='foreign'
 if mode=='named_volume':s['volumes']=['existing_named_volume']
 if mode=='lost_ack':finish(code=1)
 finish(cid)
if a[:2]==['container','inspect']:
 c=s.get('container')
 if not c or a[-1]!=cid:finish(code=1)
 fmt=value('--format')
 if '.Mounts' in fmt:finish('\n'.join(s['volumes']))
 if '.State.Status' in fmt:finish('exited|1' if mode=='process_failure' else 'exited|0')
 finish('|'.join((c['id'],'/'+c['name'],c['image'],c['token'],c['component']))+('|none' if '.HostConfig.NetworkMode' in fmt else ''))
if a[0]=='rm':
 s['removed'].append(a[-1])
 if mode=='rm_failure':finish(code=1)
 s['container']=None
 if mode!='volume_leak':s['volumes']=[]
 finish(cid)
if a[:2]==['volume','ls']:finish('\n'.join(s['volumes']))
finish(code=97)
"""

class SchemaCiResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash=str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name=='nt' else shutil.which('bash')
        if not cls.bash or not Path(cls.bash).exists():raise RuntimeError('Bash is required for resource-owner guards')

    def run_owner(self,scenario,body='schema_ci_create postgres "$image" --network none --'):
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);fake=directory/'fake.py';state=directory/'state.json'
            fake.write_text(FAKE);state.write_text(json.dumps({'calls':[],'created':0,'removed':[],'container':None,'volumes':[]}))
            env=dict(os.environ,MSYS_NO_PATHCONV='1',MSYS2_ARG_CONV_EXCL='*',FAKE_STATE=str(state),FAKE_SCENARIO=scenario,FAKE_PYTHON=Path(sys.executable).as_posix(),FAKE_DOCKER=fake.as_posix(),RESOURCE_HELPER=(ROOT/'tools/schema_ci_resources.sh').as_posix())
            script='\n'.join(('set -euo pipefail','docker() { "$FAKE_PYTHON" "$FAKE_DOCKER" "$@"; }','source "$RESOURCE_HELPER"','schema_ci_resources_init','image="postgres:17@sha256:1111111111111111111111111111111111111111111111111111111111111111"',body,''))
            result=subprocess.run([self.bash,'-c',script],env=env,capture_output=True,text=True,timeout=20)
            return result,json.loads(state.read_text())

    def test_success_and_explicit_intermediate_cleanup_remove_only_created_id(self):
        for body in ('schema_ci_create postgres "$image" --network none --','schema_ci_create fresh "$image" --network none --\nschema_ci_remove "$SCHEMA_CI_LAST_INDEX"'):
            result,state=self.run_owner('normal',body)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(state['removed'],['c'*64]);self.assertIsNone(state['container']);self.assertEqual(state['volumes'],[])
            create=next(x for x in state['calls'] if x[0]=='create');self.assertIn('--pull=never',create)
            self.assertEqual(create.count('--label'),2)

    def test_immutable_local_build_id_is_allowed_but_mutable_tag_is_rejected(self):
        for image,allowed in (('sha256:'+'1'*64,True),('leti-cook-postgres-ci:stale',False)):
            result,state=self.run_owner('normal',f'image="{image}"\nschema_ci_create postgres "$image" --network none --')
            self.assertEqual(result.returncode==0,allowed,result.stderr)
            self.assertEqual(state['created'],int(allowed))
            if allowed:
                create=next(c for c in state['calls'] if c[0]=='create')
                self.assertEqual(create[-1],image)

    def test_preexisting_or_unknown_identity_is_never_removed(self):
        for scenario in ('collision','list_failure','wrong_name','wrong_image','wrong_token','wrong_component','named_volume'):
            result,state=self.run_owner(scenario)
            self.assertNotEqual(result.returncode,0,scenario);self.assertEqual(state['removed'],[],scenario)
            if scenario in ('collision','list_failure'):self.assertEqual(state['created'],0)

    def test_lost_create_ack_recovers_only_exact_owned_identity(self):
        result,state=self.run_owner('lost_ack')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(state['removed'],['c'*64]);self.assertIsNone(state['container']);self.assertEqual(state['volumes'],[])

    def test_aggregate_timeout_signal_runs_owned_cleanup(self):
        result,state=self.run_owner('normal','schema_ci_create postgres "$image" --network none --\nkill -TERM $$')
        self.assertEqual(result.returncode,124,result.stderr)
        self.assertEqual(state['removed'],['c'*64]);self.assertIsNone(state['container']);self.assertEqual(state['volumes'],[])

    def test_cleanup_or_process_failure_cannot_report_success(self):
        for scenario in ('rm_failure','volume_leak','process_failure'):
            body='schema_ci_create postgres "$image" --network none --'
            if scenario=='process_failure':body+='\nschema_ci_assert_exited "$SCHEMA_CI_LAST_INDEX"'
            result,state=self.run_owner(scenario,body)
            self.assertNotEqual(result.returncode,0,scenario)
            self.assertEqual(state['removed'],['c'*64])

if __name__=='__main__':unittest.main()
