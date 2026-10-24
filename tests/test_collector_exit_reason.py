"""A failed collection worker leaves a fixed exit reason, never its text (audit S7-11). No database or provider."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from self_hosted_collection import read_exit_reason  # noqa: E402

KEY='AIza'+'k'*35

def reason(text):
    found={}
    read_exit_reason(io.BytesIO(text.encode()),found)
    return found.get('reason')

class ExitReasonTests(unittest.TestCase):
    def test_only_the_fixed_forms_are_kept(self):
        self.assertEqual(reason('some output\nexit_reason=KeyError\n'),'exception:KeyError')
        self.assertEqual(reason('Fatal Python error: _enter_buffered_busy: could not acquire lock\n'),'fatal_python_error')
        for text in (f'Traceback (most recent call last):\nRuntimeError: url?key={KEY}\n',
                     f'exit_reason=Bad Name {KEY}\n','exit_reason=\n',''):
            self.assertIsNone(reason(text),text)

    def test_a_large_output_is_drained_without_keeping_it(self):
        stream=io.BytesIO(('x'*1000+'\n').encode()*2000+b'exit_reason=LateError\n')
        found={}
        read_exit_reason(stream,found)
        self.assertTrue(stream.closed)
        self.assertNotIn('reason',found)   # past the parse limit; the pipe was still drained

    def test_the_worker_reports_its_exception_class(self):
        env={k:v for k,v in os.environ.items() if k not in ('LIC_COLLECTION_SUPERVISED','LIC_SELF_HOSTED_CONFIG')}
        env['LIC_DEPLOYMENT_PROFILE']='self-hosted'
        child=subprocess.run([sys.executable,'-B',str(ROOT/'application/ops/self_hosted_collect.py'),'operator'],
                             env=env,capture_output=True,timeout=60)
        self.assertEqual(child.returncode,1,child.stderr)
        found={}
        read_exit_reason(io.BytesIO(child.stderr),found)
        self.assertTrue(found.get('reason','').startswith('exception:'),child.stderr)

if __name__=='__main__':unittest.main()
