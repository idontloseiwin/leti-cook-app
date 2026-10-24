#!/usr/bin/env python3
"""Only the bounded native channel read-owner regression in an owned empty DB.

Uses the existing two-connection schema25 fixture and exact final-schema pins.
No full-suite dispatch, provider work, arbitrary relation or production DSN.
"""
import json,os,sys,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests'),str(ROOT)]
from schema25_regressions.fixtures import Fixture,admission,dsns
from schema25_regressions.cases_channel_reads import channel_readers
from schema25_regressions.manifest import require

def main():
    require(os.getenv('LIC_SCHEMA_CI_MAX_ROWS')=='1000','bounded_read_fixture_authorization')
    started=time.monotonic();fixture=None;stage='identity'
    try:
        with admission() as gate:
            fixture=Fixture(*dsns(),gate);fixture.deadline=started+230;fixture.budget=1000
            try:
                stage='channel_readers';before=fixture.checks;channel_readers(fixture)
                require(fixture.checks>before,'native_checks_required')
                stage='final_identity';fixture.identity()
            finally:fixture.close()
        print(json.dumps({'status':'pass','checks':fixture.checks,'fixture_rows_max':1000,'connections_max':2,'seconds':round(time.monotonic()-started,3),'cleanup':'verified'}));return 0
    except Exception as error:
        state=getattr(error,'code',None);frames=[]
        for frame in traceback.extract_tb(error.__traceback__)[-8:]:
            try:relative=Path(frame.filename).resolve().relative_to(ROOT).as_posix()
            except ValueError:continue
            frames.append({'file':relative,'line':frame.lineno})
        print(json.dumps({'status':'failed','stage':stage,'class':type(error).__name__,'sqlstate':state if isinstance(state,str) and state.isalnum() and len(state)==5 else None,'frames':frames}));return 1

if __name__=='__main__':raise SystemExit(main())
