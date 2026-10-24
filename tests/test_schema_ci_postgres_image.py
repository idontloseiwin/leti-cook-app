"""Run the real derived-image identity helper against a fake local Docker CLI."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
BASE='postgres:17.11-bookworm@sha256:'+'1'*64
FAKE=r"""
import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_STATE']);s=json.loads(p.read_text());a=sys.argv[1:]
s['calls'].append(a)
def finish(value='',code=0):
 p.write_text(json.dumps(s));print(value);raise SystemExit(code)
if a[0]=='build':
 tag=a[a.index('--tag')+1];label=a[a.index('--label')+1].split('=',1)[1]
 s['images'][tag]=label;finish()
if a[:2]==['image','inspect']:
 tag=a[-1]
 if tag not in s['images']:finish(code=1)
 label=s['images'][tag] if os.environ['FAKE_MODE']!='stale' else '0'*64
 identity='sha256:'+'2'*64 if os.environ['FAKE_MODE']!='mutable' else 'untrusted:tag'
 finish(identity+'|'+label)
finish(code=97)
"""

class SchemaCiPostgresImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash=str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name=='nt' else shutil.which('bash')
        if not cls.bash or not Path(cls.bash).exists():raise RuntimeError('Bash is required for image identity guards')

    def helper(self,body,mode='normal',extra=''):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'database').mkdir()
            content='FROM '+BASE+'\nRUN true\n'+extra
            (root/'database/Dockerfile').write_text(content,encoding='utf-8',newline='\n')
            fake=root/'fake.py';fake.write_text(FAKE)
            state=root/'state.json';state.write_text(json.dumps(dict(calls=[],images={})))
            env=dict(os.environ,MSYS_NO_PATHCONV='1',MSYS2_ARG_CONV_EXCL='*',FAKE_STATE=str(state),FAKE_MODE=mode,
                FAKE_PYTHON=Path(sys.executable).as_posix(),FAKE_DOCKER=fake.as_posix(),TEST_ROOT=root.as_posix(),
                IMAGE_HELPER=(ROOT/'tools/schema_ci_postgres_image.sh').as_posix())
            script='\n'.join(('set -euo pipefail','docker() { "$FAKE_PYTHON" "$FAKE_DOCKER" "$@"; }',
                'source "$IMAGE_HELPER"',body,''))
            result=subprocess.run([self.bash,'-c',script],env=env,capture_output=True,text=True,timeout=20)
            return result,json.loads(state.read_text()),hashlib.sha256(content.encode()).hexdigest()

    def test_build_and_use_exact_source_as_immutable_image_without_network(self):
        result,state,digest=self.helper('schema_ci_postgres_build "$TEST_ROOT"\nschema_ci_postgres_require "$TEST_ROOT"\nprintf "%s\\n" "$SCHEMA_CI_POSTGRES_IMAGE"')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'sha256:'+'2'*64)
        build=state['calls'][0]
        self.assertEqual(build[:5],['build','--network','none','--pull=false','--tag'])
        self.assertIn('leti-cook-postgres-ci:'+digest,build)
        self.assertIn('org.leti-cook.postgres-source-sha='+digest,build)
        self.assertFalse(any(c[0]=='pull' for c in state['calls']))

    def test_source_change_gets_distinct_tag_and_cannot_reuse_stale_build(self):
        body='schema_ci_postgres_build "$TEST_ROOT"\nprintf "\\n# changed\\n" >> "$TEST_ROOT/database/Dockerfile"\nschema_ci_postgres_require "$TEST_ROOT"'
        result,state,digest=self.helper(body)
        self.assertNotEqual(result.returncode,0)
        self.assertNotEqual(state['calls'][0][5],state['calls'][-1][-1])

    def test_missing_wrong_label_and_nonimmutable_image_fail_closed(self):
        for mode in ('missing','stale','mutable'):
            body=('' if mode=='missing' else 'schema_ci_postgres_build "$TEST_ROOT"\n')+'schema_ci_postgres_require "$TEST_ROOT"'
            result,state,_=self.helper(body,mode)
            self.assertNotEqual(result.returncode,0,mode)
            self.assertFalse(any(c[0]=='create' for c in state['calls']))

    def test_unhashed_build_inputs_are_rejected_before_build(self):
        for extra in ('COPY wrapper.sh /usr/local/bin/\n','ADD something /tmp/\n','ARG EXTRA\n'):
            result,state,_=self.helper('schema_ci_postgres_build "$TEST_ROOT"',extra=extra)
            self.assertNotEqual(result.returncode,0,extra)
            self.assertEqual(state['calls'],[])

if __name__=='__main__':unittest.main()
