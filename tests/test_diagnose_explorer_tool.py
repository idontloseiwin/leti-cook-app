"""Offline CLI import, read protocol and resource lifetime for explorer diagnosis."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
from pathlib import Path
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
PATH=ROOT/'tools/diagnose_explorer_500.py'


def load():
    spec=importlib.util.spec_from_file_location('diagnose_explorer_test',PATH)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiagnosticTests(unittest.TestCase):
    def test_import_and_help_have_no_runtime_or_thread_side_effect(self):
        with patch('threading.Timer') as timer:
            module=load()
            with patch.object(module,'_load_runtime') as runtime, redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stop:
                module.main(['--help'])
            self.assertEqual(stop.exception.code,0)
            runtime.assert_not_called()
            timer.assert_not_called()

    def test_named_reads_use_exact_cursor_and_bounded_batches(self):
        module=load(); calls=[]
        class Repository:
            def load_channel(self, channel, **kw): calls.append(('channel',channel,kw));return {}
            def _inventory_page(self, channel, **kw):
                calls.append(('page',channel,kw))
                if kw['cursor_video_id'] is not None:return []
                return [{'video_id':f'{index:011d}','published_at':None} for index in range(1000)]
            def count_canonical_inventory(self,channel,**kw):calls.append(('count',channel,kw));return 1000
            def load_videos_by_ids(self,ids,**kw):calls.append(('videos',ids,kw));return []
        explorer=types.SimpleNamespace(CHANNEL_COLUMNS='channel_id',FORMAT_TOTAL_VIDEO_COLUMNS='video_id',VIDEO_COLUMNS='video_id,views',
            build_channel_format_totals=lambda *args:{'formats':{'long':{'status':'ready'}}},
            build_channel_explorer=lambda *args,**kwargs:{'ok':True})
        output=[]
        result=module.diagnose(Repository(),module.DEFAULT_CHANNEL,explorer,
            lambda row:(None,row['video_id']),{},emit=output.append)
        self.assertEqual(result,0)
        pages=[call[2] for call in calls if call[0]=='page']
        self.assertEqual(len(pages),2)
        self.assertEqual(pages[1],{'video_type':'long','page_size':1000,'cursor_published':None,'cursor_video_id':'00000000999','strict':True})
        self.assertEqual([len(call[1]) for call in calls if call[0]=='videos'],[500,500])
        self.assertFalse(any('00000000999' in line for line in output))
        self.assertNotIn('inventory_page_2_quoted','\n'.join(output))

    def test_shared_scope_closes_and_never_starts_quota_recorder(self):
        module=load(); events=[];database=object()
        @contextmanager
        def resources(**kwargs):
            events.append(('open',kwargs))
            try:yield database
            finally:events.append(('close',))
        def repository(value):
            self.assertIs(value,database)
            raise RuntimeError('private-row-and-dsn')
        with patch.object(module,'_load_runtime',return_value=(resources,repository,None,None)), patch.object(module.threading,'Timer') as timer, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main([]),1)
        self.assertEqual(events,[('open',{'record_quota':False}),('close',)])
        timer.return_value.cancel.assert_called_once()
        self.assertNotIn('private-row-and-dsn',output.getvalue())

    def test_invalid_input_fails_before_resources(self):
        module=load()
        for arguments in (['--channel','invalid'],['--time-limit-seconds','0'],['--time-limit-seconds','61']):
            with self.subTest(arguments=arguments), patch.object(module,'_load_runtime') as runtime, patch.object(module.threading,'Timer') as timer, patch('sys.stderr',io.StringIO()), self.assertRaises(SystemExit) as stop:
                module.main(arguments)
            self.assertEqual(stop.exception.code,2)
            runtime.assert_not_called();timer.assert_not_called()

    def test_stage_failure_continues_safely_and_code_is_sanitized(self):
        module=load()
        error=RuntimeError('secret-payload')
        error.code='postgresql://secret'
        self.assertNotIn('secret',module.safe(error))
        self.assertIn('code=None',module.safe(error))
        source=PATH.read_text(encoding='utf-8-sig')
        self.assertNotIn('SUPABASE_',source)
        self.assertNotIn('.table(',source)
        self.assertNotIn('admission=',source)


if __name__=='__main__':unittest.main()
