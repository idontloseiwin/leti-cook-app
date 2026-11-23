"""Optional-field policy, provider evidence and dependency safety."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from research_field_policy import *
from collection.optional_fields import observe,payload,PROOF_KEY
from research_query.validation import AccessContext,validate,canonical as query_bytes,QueryRejected
from test_research_query_contract import query
from research_query.saved import from_query
from user_work_backup import normalize_backup,encode_backup,decode_backup,import_payload
from test_backup_preview_parity import document
from db.reads.research_sql import compile_query

class FieldTests(unittest.TestCase):
 def test_defaults_and_hierarchy(self):
  doc=defaults();self.assertEqual(set(effective(doc,'short').values()),{'off'})
  doc['workspace']['video.description']='current'
  doc['sources'][SOURCE]={'fields':{'video.paid_promotion':'current'},'types':{'short':{'video.description':'off'}}}
  self.assertEqual(effective(doc,'long'),dict(zip(FIELDS,['current','current'])))
  self.assertEqual(effective(doc,'short')['video.description'],'off')
  for bad in [dict(doc,version=True),dict(doc,lifecycle_profile='unlimited'),dict(doc,workspace={'video.id':'off'}),dict(doc,sources={'private':{}})]:
   with self.assertRaises(FieldPolicyError):normalize(bad)
 def test_capture_and_provider_text_cannot_forge_policy(self):
  proof=capture(defaults(),1)
  item={'id':'abcdefghijk','snippet':{'description':'private text','title':'T'},'paidProductPlacementDetails':{'hasPaidProductPlacement':True},PROOF_KEY:{'forged':True}}
  sanitized=observe([item],proof)[0]
  self.assertNotIn('description',sanitized['snippet']);self.assertNotIn('paidProductPlacementDetails',sanitized)
  self.assertEqual(payload(sanitized)['optional_observation'],{'proof':proof,'values':{}})
  self.assertEqual(item['snippet']['description'],'private text')
  doc=defaults();doc['workspace']={field:'current' for field in FIELDS}
  sanitized=observe([item],capture(doc,2))[0];self.assertEqual(sanitized[PROOF_KEY]['values'],dict(zip(FIELDS,['private text',True])))
  missing=observe([{'id':'abcdefghijk','snippet':{'description':''},'paidProductPlacementDetails':{}}],capture(doc,2))[0]
  self.assertEqual(missing[PROOF_KEY]['values']['video.description'],'');self.assertIsNone(missing[PROOF_KEY]['values']['video.paid_promotion'])
  bad=deepcopy(proof);bad['version']=True
  with self.assertRaises(FieldPolicyError):validate_capture(bad)
 def test_purge_requires_every_format_disabled(self):
  doc=defaults();self.assertEqual(purge_fields(list(FIELDS),doc),list(FIELDS))
  for bad in [[{}],['video.id'],[FIELDS[0],FIELDS[0]]]:
   with self.assertRaises(FieldPolicyError):purge_fields(bad,doc)
  doc['sources'][SOURCE]={'fields':{},'types':{'stream':{FIELDS[0]:'current'}}}
  with self.assertRaises(FieldPolicyError):purge_fields([FIELDS[0]],doc)
 def test_query_disabled_by_type_and_saved_definition_retained(self):
  q=query(select=['video.description'],video_types=['short'])
  context=AccessContext('fixture','owner',enabled=True,disabled_type_fields=frozenset({('short','video.description')}))
  with self.assertRaisesRegex(QueryRejected,'field_disabled'):validate(query_bytes(q),context)
  validate(query_bytes({**q,'video_types':['long']}),context)
  _,definition=from_query(q);self.assertEqual(definition['query']['select'],q['select'])
  self.assertNotEqual(context.fingerprint(),replace(context,preferences_revision='2').fingerprint())
 def test_optional_sql_only_when_needed_and_separate_clocks(self):
  context=AccessContext('fixture','owner',enabled=True)
  sql,_=compile_query(validate(query_bytes(query(select=['video.description','video.paid_promotion'],where={'field':'video.description','op':'contains_casefold_v1','value':'Straße'})),context))
  self.assertIn('readable.video_optional_metadata',sql);self.assertIn('o.video_type=%(video_type)s',sql)
  # One protected-view lookup per row: a plain join compared every row with every description.
  # The searched field is read for the whole scope; the field only shown, for the page.
  self.assertIn('LEFT JOIN LATERAL (SELECT o.description,o.description_checked_at,o.description_folded FROM readable.video_optional_metadata o WHERE o.video_id=p.video_id AND o.video_type=%(video_type)s::text LIMIT 1) optional ON true',sql)
  self.assertIn('LEFT JOIN LATERAL (SELECT o.paid_promotion,o.paid_promotion_checked_at FROM readable.video_optional_metadata o WHERE o.video_id=page.video_id AND o.video_type=%(video_type)s::text LIMIT 1) page_optional ON true',sql)
  self.assertIn('(SELECT min(optional_expiry) FROM details)',sql)
  self.assertNotIn('JOIN readable.video_optional_metadata optional ON',sql)
  shown=validate(query_bytes(query(select=['video.description'])),context)
  self.assertNotIn(') optional ON true',compile_query(shown)[0]);self.assertIn('page_optional ON true',compile_query(shown)[0])
  # Summaries read the whole population, so they keep the scope join.
  self.assertIn(') optional ON true',compile_query(shown,population=True)[0]);self.assertNotIn('page_optional',compile_query(shown,population=True)[0])
  sorted_by=validate(query_bytes(query(select=['video.description'],order_by=[{'field':'video.description','direction':'asc','nulls':'last'}])),context)
  self.assertNotIn('page_optional',compile_query(sorted_by)[0])
  self.assertIn('description_checked_at',sql);self.assertIn('paid_promotion_checked_at',sql)
  self.assertNotIn('video_optional_metadata',compile_query(validate(query_bytes(query()),context))[0])
 def test_backup_choices_roundtrip_without_provider_values(self):
  backup=normalize_backup(document());backup['user_work']['field_preferences']['workspace']['video.description']='current'
  encoded=encode_backup(backup);self.assertEqual(decode_backup(encoded),backup)
  self.assertEqual(import_payload(backup)['field_preferences'],backup['user_work']['field_preferences'])
  bad=deepcopy(backup);bad['user_work']['field_preferences']['rows']=[{'description':'copied'}]
  with self.assertRaises(Exception):normalize_backup(bad)

if __name__=='__main__':unittest.main()
