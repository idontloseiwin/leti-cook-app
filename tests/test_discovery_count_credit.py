"""Optional count credit must never reject an otherwise discoverable ID."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.discovery_counts import credit_candidates
from db.discovery_activation import PreparationContract,_host,_formats
from db.base.postgres import DatabaseServerError

NOW=datetime(2026,11,9,12,tzinfo=timezone.utc)


class CreditTests(unittest.TestCase):
    def test_only_actual_first_canonical_inserts_get_publication_evidence(self):
        items=[dict(video_id='new',published_at=NOW.isoformat()),dict(video_id='known',published_at=NOW.isoformat())]
        self.assertEqual(credit_candidates(['new'],items,NOW),[dict(video_id='new',published_at=NOW.isoformat())])
        self.assertEqual(credit_candidates([],items,NOW),[])

    def test_invalid_unknown_future_or_naive_publications_lose_only_optional_credit(self):
        for value in (None,'','unknown','infinity','2026-11-09T12:00:00',NOW.replace(tzinfo=None),
                      (NOW+timedelta(seconds=1)).isoformat(),'0001-01-01T00:00:00+23:00'):
            with self.subTest(value=value):
                self.assertEqual(credit_candidates(['new'],[dict(video_id='new',published_at=value)],NOW),[])

    def test_original_source_clock_and_timezone_are_used_not_wallclock(self):
        historical=NOW-timedelta(days=1)
        source=historical.isoformat()
        self.assertEqual(credit_candidates(['new'],[dict(video_id='new',published_at=NOW.isoformat())],source),[])
        shifted=historical.astimezone(timezone(timedelta(hours=2))).isoformat()
        self.assertEqual(credit_candidates(['new'],[dict(video_id='new',published_at=shifted)],source),
                         [dict(video_id='new',published_at=historical.isoformat())])

    def test_absent_source_or_items_are_not_invented(self):
        self.assertEqual(credit_candidates(['new'],[dict(video_id='new')],NOW),[])
        self.assertEqual(credit_candidates(['new'],[dict(video_id='new',published_at=NOW.isoformat())],None),[])
        with self.assertRaises(DatabaseServerError):credit_candidates([], [{}]*51,NOW)


class ActivationContractTests(unittest.TestCase):
    def test_no_implicit_or_partial_host_preparation(self):
        valid=PreparationContract(True,True,True,True,True)
        self.assertIsNone(_host(valid))
        for value in (None,True,{},replace(valid,full_admission=False),replace(valid,workers_drained=False),
                      replace(valid,count_head_admission=1)):
            with self.subTest(value=value),self.assertRaises(DatabaseServerError):_host(value)

    def test_both_mandatory_sources_are_required(self):
        self.assertEqual(_formats(('long','short')),['long','short'])
        self.assertEqual(_formats(('long','short','stream')),['long','short','stream'])
        for value in ((),('long',),('short',),('stream',),('long','short','short'),('long','short','other')):
            with self.subTest(value=value),self.assertRaises(DatabaseServerError):_formats(value)


if __name__=='__main__':unittest.main()
