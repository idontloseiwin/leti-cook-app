"""manage.py check-secrets reports locations only and never writes. No Docker or database."""
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import manage

KEY='AIza'+'k'*35
COLUMNS=('public\tchannels\tchannel_metadata_error\t["channel_id"]\n'
         'public\tchannels\tdeep_inventory_error\t["channel_id"]\n'
         'operator_private\tprovider_log\tsource_url\t["source_url"]\n'
         'public\tnotes\tbody\t[]\n')

class FakeRun:
    """Returns the prepared outputs in order; an exception instance is raised instead."""
    def __init__(self,findings,columns=COLUMNS):
        self.outputs,self.calls,self.timeouts=[columns,findings],[],[]
    def __call__(self,args,*,cwd=None,timeout=300):
        self.calls.append(args);self.timeouts.append(timeout)
        output=self.outputs[len(self.calls)-1]
        if isinstance(output,BaseException):raise output
        return output

def check(findings,columns=COLUMNS):
    run,lines=FakeRun(findings,columns),[]
    code=manage.check_secrets(Path('/opt/leti-cook'),['docker','compose'],run=run,emit=lines.append)
    return code,run,'\n'.join(lines)

class CheckSecretsTests(unittest.TestCase):
    def test_nothing_found(self):
        code,run,output=check('')
        self.assertEqual(code,0)
        self.assertIn('no YouTube API key found in 4 text columns',output)

    def test_findings_show_locations_but_never_values(self):
        code,_,output=check('public\tchannels\tchannel_metadata_error\tUC'+'a'*22+'\n'
                            'public\tchannels\tdeep_inventory_error\thttps://x.test/?key='+KEY+'\n'
                            'operator_private\tprovider_log\tsource_url\t(row key hidden)\n')
        self.assertEqual(code,3)
        self.assertIn('public.channels.channel_metadata_error: 1 rows; row keys: UC'+'a'*22,output)
        self.assertIn('public.channels.deep_inventory_error: 1 rows; row keys: (row key hidden)',output)
        self.assertIn('operator_private.provider_log.source_url: 1 rows',output)
        self.assertNotIn(KEY,output)
        self.assertNotIn('key=',output)

    def test_many_rows_are_capped(self):
        rows=''.join(f'public\tchannels\tchannel_metadata_error\tUC{i:022d}\n' for i in range(manage.FINDINGS_PER_COLUMN+1))
        _,_,output=check(rows)
        self.assertIn(f': {manage.FINDINGS_PER_COLUMN}+ rows',output)
        self.assertEqual(output.count('UC'),manage.FINDINGS_PER_COLUMN)

    def test_database_session_is_read_only_and_queries_only_read(self):
        _,run,_=check('')
        for args in run.calls:
            options=next(a for a in args if a.startswith('PGOPTIONS='))
            self.assertIn('-c default_transaction_read_only=on',options)
            self.assertIn('ON_ERROR_STOP=1',args)
            self.assertEqual(args[args.index('exec')+1:args.index('exec')+2],['-T'])
        for sql in (run.calls[0][-1],run.calls[1][-1]):
            self.assertIsNone(re.search(r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|COPY)\b|;',sql))

    def test_database_cancels_slow_statements_and_lock_waits_first(self):
        _,run,_=check('')
        options=next(a for a in run.calls[1] if a.startswith('PGOPTIONS='))
        self.assertIn(f'-c statement_timeout={manage.SCAN_STATEMENT_TIMEOUT_SECONDS*1000}',options)
        self.assertIn(f'-c lock_timeout={manage.SCAN_LOCK_TIMEOUT_SECONDS*1000}',options)
        self.assertGreater(run.timeouts[1],manage.SCAN_STATEMENT_TIMEOUT_SECONDS)

    def test_an_unfinished_scan_is_never_reported_clean(self):
        cases={'timeout':subprocess.TimeoutExpired('docker',1),
               'command_failed_3':manage.InstallError('command_failed_3'),  # e.g. statement_timeout in psql
               'unexpected_database_output':'too\tfew\n'}
        for reason,outcome in cases.items():
            code,_,output=check(outcome)
            self.assertEqual(code,manage.EXIT_INCOMPLETE,reason)
            self.assertIn(f'did not finish ({reason})',output)
            self.assertNotIn('no YouTube API key found',output)
        for columns in (subprocess.TimeoutExpired('docker',1),''):
            code,_,output=check('',columns=columns)
            self.assertEqual(code,manage.EXIT_INCOMPLETE)
            self.assertNotIn('no YouTube API key found',output)

    def test_findings_point_to_the_supported_cleanup(self):
        code,_,output=check('public\tchannels\tchannel_metadata_error\tUC'+'a'*22+'\n')
        self.assertEqual(code,manage.EXIT_FOUND)
        self.assertNotIn('migration',output)
        self.assertIn('Run manage.py redact-secrets',output)
        self.assertIn('do not delete other rows by hand',output)

    def test_cli_turns_a_timeout_into_a_failure_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'installed.json').write_text(json.dumps({'project':'leti-cook-0123456789ab'}))
            def timed_out(*a,**k):raise subprocess.TimeoutExpired('docker',1)
            stderr=io.StringIO()
            with patch.object(manage,'__file__',str(Path(directory,'manage.py'))),\
                 patch.object(manage,'safe_path',Path),patch.object(manage,'check_docker',lambda:None),\
                 patch.object(manage,'compose_prefix',lambda *a:['docker']),patch.object(manage,'check_secrets',timed_out),\
                 patch('ops.updater.management',lambda *a:contextlib.nullcontext((None,False))),\
                 patch.object(sys,'argv',['manage.py','check-secrets']),contextlib.redirect_stderr(stderr):
                self.assertEqual(manage.main(),1)
            self.assertIn('Command failed',stderr.getvalue())

    def test_scan_query_quotes_names_and_hides_key_columns(self):
        sql=manage.scan_sql([('public','we"ird','co\'l',['id']),('public','t','source_url',['source_url']),('public','n','body',[])])
        self.assertIn('FROM "public"."we""ird" WHERE "co\'l"::text ~',sql)
        self.assertIn("'co''l'",sql)
        self.assertIn("'(row key hidden)' FROM \"public\".\"t\"",sql)
        self.assertIn("'(no primary key)' FROM \"public\".\"n\"",sql)
        self.assertEqual(sql.count('UNION ALL'),2)
        self.assertEqual(sql.count(f'LIMIT {manage.FINDINGS_PER_COLUMN+1}'),3)

    def test_pattern(self):
        pattern=re.compile(manage.SECRET_PATTERN)
        for text in (KEY,'x '+KEY+' y','GET /youtube/v3/videos?part=id&key=abc','/v3/videos?key=abc'):
            self.assertIsNotNone(pattern.search(text),text)
        for text in ('monkey=1','AIza-too-short','keys: none','api_key_required'):
            self.assertIsNone(pattern.search(text),text)

    def test_command_is_available_without_a_terminal(self):
        self.assertIn('check-secrets',manage.SCRIPTABLE_ACTIONS)
        source=(ROOT/'manage.py').read_text(encoding='utf-8')
        self.assertIn("if args.action not in SCRIPTABLE_ACTIONS and not sys.stdin.isatty():",source)

if __name__=='__main__':unittest.main()
