"""Saved definitions are portable user work, not observations or permission grants."""
from copy import deepcopy
import unittest
from test_research_query_contract import query
from test_backup_label_names import document
from research_query.saved import from_query, bound_query, normalize_definition
from research_query.validation import AccessContext, QueryRejected, canonical, validate
from user_work_backup import normalize_backup, encode_backup, decode_backup, import_payload
from saved_work import normalize_conditions

class SavedQueryTests(unittest.TestCase):
    def test_definition_roundtrip_keeps_window_without_grant(self):
        q=query(select=['observation.views_per_hour'],observations={'version':1,'window_seconds':86400,'tolerance_seconds':7200})
        channel,definition=from_query(q)
        self.assertEqual(normalize_definition(definition),definition)
        resolved=bound_query(definition,channel)
        self.assertEqual(resolved['observations']['window_seconds'],86400)
        with self.assertRaises(QueryRejected) as failure:
            validate(canonical(resolved),AccessContext('fixture','owner',enabled=True))
        self.assertEqual(failure.exception.code,'grant_required')
    def test_channel_binding_has_no_baked_in_provider_identity(self):
        original='UC'+'1'*22; replacement='UC'+'2'*22
        channel,definition=from_query(query(scope={'kind':'channel','id':original}))
        self.assertEqual(channel,original)
        self.assertEqual(definition['query']['scope']['id'],'$channel')
        self.assertNotIn(original,str(definition))
        self.assertEqual(bound_query(definition,replacement)['scope']['id'],replacement)
    def test_cursor_results_and_extra_payload_are_never_saved(self):
        for key,value in [('rows',[]),('last_result',{}),('token','secret')]:
            q=query();q[key]=value
            with self.assertRaises(ValueError):from_query(q)
        q=query(page={'size':25,'after':'opaque'})
        with self.assertRaises(ValueError):from_query(q)
    def test_version_three_roundtrip_and_old_backup_inputs(self):
        legacy=normalize_backup(document())
        self.assertEqual(legacy['version'],6)
        self.assertEqual(legacy['user_work']['queries'],[])
        _,definition=from_query(query())
        view=dict(id='c0000000-0000-4000-8000-000000000001',name='My query',revision=1,
                  created_at='2026-10-01T00:00:00Z',updated_at='2026-10-01T00:00:00Z',conditions=definition)
        # A deleted folder remains a blocked definition; it must not become all channels.
        legacy['user_work']['queries']=[view]
        normalized=normalize_backup(legacy)
        self.assertEqual(decode_backup(encode_backup(normalized)),normalized)
        self.assertIn('create_fingerprint',import_payload(normalized)['queries'][0])
        for forbidden in ('rows','views_count','api_key'):
            bad=deepcopy(legacy);bad['user_work']['queries'][0][forbidden]='x'
            with self.assertRaises(Exception):normalize_backup(bad)
    def test_execution_mode_is_never_saved_and_old_objects_still_open(self):
        """The browser codec accepts only consistency current and page {size}."""
        _,plain=from_query(query(page={'size':25}))
        self.assertEqual((plain['query']['consistency'],plain['query']['page']),('current',{'size':25}))
        # Write path: a frozen Library request saved as is keeps only the definition.
        _,saved=from_query(query(consistency='frozen',page={'size':25,'anchor_id':'abcdefghijk'}))
        self.assertEqual(saved,plain)
        # Read path: an object stored before this rule opens as the same current query.
        stored=deepcopy(plain);stored['query']['consistency']='frozen';stored['query']['page']['anchor_id']='abcdefghijk'
        for normalized in (normalize_definition(stored,allow_unknown=True),normalize_conditions(stored)):
            self.assertEqual(normalized,plain)
        self.assertEqual(bound_query(stored,None)['consistency'],'current')
        self.assertNotIn('anchor_id',bound_query(stored,None)['page'])
        # A research definition (v3) carries its query through the same normalizer.
        from research_query.graph import normalize_saved_research
        from test_research_graph import graph_document
        research=dict(version=3,query=from_query(query())[1]['query'],graph=graph_document('table')['graph'])
        frozen_research=deepcopy(research);frozen_research['query']['consistency']='frozen'
        self.assertEqual(normalize_saved_research(frozen_research),normalize_saved_research(research))
        self.assertEqual(normalize_saved_research(frozen_research)['query']['consistency'],'current')
        # Still refused: a cursor, an unknown mode and an anchor without a frozen read.
        for bad in (query(page={'size':25,'after':'opaque'}),query(consistency='later'),query(page={'size':25,'anchor_id':'abcdefghijk'})):
            with self.subTest(query=bad),self.assertRaises(Exception):from_query(bad)
    def test_unknown_field_stays_inert_and_readable(self):
        _,definition=from_query(query())
        definition['query']['select']=['video.future_field']
        preserved=normalize_conditions(definition)
        self.assertEqual(preserved['query']['select'],['video.future_field'])
        with self.assertRaises(QueryRejected):
            validate(canonical(bound_query(preserved,None)),AccessContext('fixture','owner',enabled=True))


LIBRARY={'version':1,'surface':'library','view':'grid'}


class PresentationTests(unittest.TestCase):
    """A v2 Library save: source, channel scope, type, conditions, multi-sort and display."""
    def channel_query(self,**changes):
        return query(**{'scope':{'kind':'channel','id':'UC'+'1'*22},'select':['video.id','video.title','video.views','video.published_at'],
                     'where':{'all':[{'field':'video.title','op':'contains_casefold_v1','value':'a b'},{'field':'video.views','op':'gte','value':'10'}]},
                     'order_by':[{'field':'video.views','direction':'desc'},{'field':'video.published_at','direction':'asc'}],**changes})
    def test_old_definitions_stay_valid_and_unchanged(self):
        _,definition=from_query(query())
        self.assertNotIn('presentation',definition)
        self.assertEqual(normalize_definition(definition),definition)
        self.assertEqual(normalize_conditions(definition),definition)
    def test_library_presentation_roundtrip_carries_what_library_needs(self):
        presentation={**LIBRARY,'density':'compact','columns':['video.title','video.views'],'time_zone':'Europe/Berlin'}
        channel,definition=from_query(self.channel_query(video_types=['short']),presentation)
        self.assertEqual(channel,'UC'+'1'*22)
        self.assertEqual(definition['presentation'],presentation)
        self.assertEqual(definition['query']['video_types'],['short'])
        self.assertEqual([o['field'] for o in definition['query']['order_by']],['video.views','video.published_at'])
        self.assertEqual(normalize_definition(definition),definition)
        self.assertEqual(normalize_conditions(definition),definition)
        bound=bound_query(definition,'UC'+'2'*22)
        self.assertNotIn('presentation',bound)
        self.assertEqual(bound['scope']['id'],'UC'+'2'*22)
        self.assertEqual(from_query(self.channel_query(),LIBRARY)[1]['presentation'],LIBRARY)
    def test_presentation_is_closed_and_strict(self):
        _,base=from_query(self.channel_query(),LIBRARY)
        bad=[{**LIBRARY,'extra':1},{**LIBRARY,'version':2},{**LIBRARY,'version':True},{'version':1,'surface':'library'},
             {**LIBRARY,'surface':'workshop'},{**LIBRARY,'view':'list'},{**LIBRARY,'density':'cosy'},
             {**LIBRARY,'columns':[]},{**LIBRARY,'columns':['video.likes']},{**LIBRARY,'columns':['video.id','video.id']},
             {**LIBRARY,'columns':'video.id'},{**LIBRARY,'columns':['video.id']*51},{**LIBRARY,'time_zone':'local'},
             {**LIBRARY,'time_zone':'Europe/'+'x'*60},{**LIBRARY,'time_zone':None},[],None,'library']
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):normalize_definition({**base,'presentation':value})
                with self.assertRaises(Exception):normalize_conditions({**base,'presentation':value})
        with self.assertRaises(ValueError):normalize_definition({**base,'unrelated':{}})
        _,folder=from_query(query())
        with self.assertRaises(ValueError):normalize_definition({**folder,'presentation':LIBRARY})
        channel_source=query(source={'id':'public.channels.current.v1'},scope={'kind':'channel','id':'UC'+'1'*22},
                             select=['channel.id'],order_by=[{'field':'channel.id','direction':'asc'}])
        channel_source.pop('video_types')
        _,channels=from_query(channel_source)
        with self.assertRaises(ValueError):normalize_definition({**channels,'presentation':LIBRARY})
    def test_unknown_but_well_formed_zone_is_kept_only_as_stored_work(self):
        _,base=from_query(self.channel_query(),LIBRARY)
        value={**base,'presentation':{**LIBRARY,'time_zone':'Mars/Olympus_Mons'}}
        with self.assertRaises(ValueError):normalize_definition(value)
        self.assertEqual(normalize_definition(value,allow_unknown=True)['presentation']['time_zone'],'Mars/Olympus_Mons')
    def test_presentation_counts_toward_the_size_limit(self):
        select=['video.'+name for name in ('id','title','views','published_at','duration_seconds','likes','comments','category_id','tags','topic_urls','audio_language','metadata_language','caption_available','scheduled_start_at','actual_end_at','thumbnail_url','channel_id','type')]
        presentation={**LIBRARY,'columns':select,'density':'compact','time_zone':'America/Argentina/Buenos_Aires'}
        def sized(length):
            leaves=[{'field':'video.title','op':'contains_casefold_v1','value':str(i)*1000} for i in range(6)]
            return self.channel_query(select=select,where={'any':leaves+[{'field':'video.title','op':'contains_casefold_v1','value':'y'*length}]})
        def size(length):
            try:return len(canonical(from_query(sized(length))[1]))
            except ValueError:return 8193
        length=max(n for n in range(0,1001,10) if size(n)<8192-len(canonical(presentation))//2)
        _,definition=from_query(sized(length))
        self.assertLess(len(canonical(definition)),8192)
        self.assertGreater(len(canonical({**definition,'presentation':presentation})),8192)
        with self.assertRaises(ValueError):from_query(sized(length),presentation)
        with self.assertRaises(ValueError):normalize_definition({**definition,'presentation':presentation},allow_unknown=True)
    def test_backup_roundtrip_keeps_presentation(self):
        backup=normalize_backup(document())
        _,definition=from_query(self.channel_query(),{**LIBRARY,'time_zone':'UTC','density':'large'})
        backup['user_work']['channels']=[dict(id='c0000000-0000-4000-8000-000000000008',user_input=None,folder_id=None,parent_ref=None,
            is_hidden=False,added_at='2026-10-01T00:00:00+00:00',label_keys=[],saved_views=[dict(id='c0000000-0000-4000-8000-000000000009',
            name='Library save',revision=1,created_at='2026-10-01T00:00:00Z',updated_at='2026-10-01T00:00:00Z',conditions=definition)])]
        normalized=normalize_backup(backup)
        self.assertEqual(normalized['user_work']['channels'][0]['saved_views'][0]['conditions'],definition)
        self.assertEqual(decode_backup(encode_backup(normalized)),normalized)
        self.assertEqual(import_payload(normalized)['channels'][0]['saved_views'][0]['conditions'],definition)

if __name__=='__main__':unittest.main()
