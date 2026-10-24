"""Exercise every owner of the retired channel view through actual SQL guards."""
import ast,re,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from db.base.postgres import _reviewed_statement
from db.reads.frontend import FrontendRepository
from db.channel.channel_sync import ChannelSyncRepository
from db.channel.channel_discovery import ChannelDiscoveryRepository
from db.inventory.inventory_maintenance import InventoryMaintenanceRepository
from db.reads.system_health import SystemHealthRepository,CHANNEL_HEALTH_COLUMNS,CHANNEL_HEALTH_ORDER
from db.channel.channel_discovery_projection import COLUMNS

# Output columns, in order, of the retired view public.channels_with_discovery.
RETIRED_VIEW_COLUMNS=(
    'channel_id', 'title', 'subscribers', 'views', 'video_count',
    'added_at', 'avatar_url', 'folder_id', 'parent_channel_id', 'uploads_playlist_id',
    'deep_bypass', 'deep_bypass_reason', 'deep_inventory_status', 'deep_inventory_page_token', 'deep_inventory_started_at',
    'deep_inventory_checked_at', 'deep_inventory_error', 'long_video_count', 'initial_long_inventory_status', 'initial_28d_stats_status',
    'is_hidden', 'hidden_at', 'initial_long_inventory_started_at', 'initial_28d_stats_started_at', 'channel_metadata_checked_at',
    'channel_metadata_next_after', 'channel_metadata_status', 'channel_metadata_started_at', 'channel_metadata_error', 'channel_metadata_error_count',
    'channel_video_count_changed_at', 'country_code', 'country_collected_at', 'initial_long_inventory_error_code', 'initial_long_inventory_error_at',
    'initial_long_inventory_error_count', 'channel_created_at', 'channel_discovery_checked_at', 'channel_discovery_next_after', 'channel_discovery_status',
    'channel_discovery_started_at', 'channel_discovery_resume_page_token', 'channel_discovery_resume_started_at', 'channel_discovery_no_new_streak', 'channel_discovery_error',
    'channel_discovery_error_count', 'initial_inventory_estimate', 'collection_permission', 'collection_control_epoch', 'collection_mode')
# Views dropped when Python took over their owners; no active SQL may name them.
RETIRED_VIEWS={'channels_with_discovery', 'typed_video_collection_candidates'}

class Recorded:
    def __init__(self):self.calls=[]
    def read_rows(self,name,query,params=(),*,max_rows):
        self._check(name,query,params,max_rows);return []
    def read_row(self,name,query,params=()):
        self._check(name,query,params,1);return None
    def _check(self,name,query,params,maximum):
        checked=_reviewed_statement(query,read=True,max_rows=maximum)
        bound=PostgresQuery(Transformer());bound.convert(checked,params)
        self.calls.append((name,query if isinstance(query,str) else query.as_string(),tuple(params),maximum))

class ProjectionOwnerTests(unittest.TestCase):
    def test_every_actual_owner_keeps_values_bounds_and_native_binding(self):
        cases=(
          (lambda d:FrontendRepository(d).load_channel_rows('channel_id,is_hidden'),(),10000),
          (lambda d:FrontendRepository(d).load_channel('opaque'),('opaque',),1),
          (lambda d:ChannelSyncRepository(d).load_channel_record('opaque'),('opaque',),1),
          (lambda d:ChannelSyncRepository(d).load_running_status_page(channel_id='opaque',page=2,page_size=7),('opaque',7,14),7),
          (lambda d:ChannelDiscoveryRepository(d).load_timing('opaque'),('opaque',),1),
          (lambda d:InventoryMaintenanceRepository(d).load_channels('channel_id'),(),10000),
          (lambda d:InventoryMaintenanceRepository(d).load_channels_page('channel_id',offset=4,page_size=7,order_column='title'),(7,4),7),
          (lambda d:InventoryMaintenanceRepository(d).fetch_all_rows('channels','channel_id','title',page_size=7),(7,0),7),
          (lambda d:SystemHealthRepository(d).fetch_paginated('channels',CHANNEL_HEALTH_COLUMNS,operations=CHANNEL_HEALTH_ORDER,page_size=7,max_rows=7),(7,0),7))
        for action,params,maximum in cases:
            with self.subTest(action=action):
                d=Recorded();action(d);self.assertEqual(len(d.calls),1)
                _,query,actual,bound=d.calls[0]
                self.assertEqual((actual,bound),(params,maximum))
                self.assertNotIn('public.channels_with_discovery',query)
                source = 'readable' if d.calls[0][0].startswith(('frontend_','system_health_')) else 'public'
                self.assertIn(source + '.channels c',query)
                self.assertIn(source + '.channel_playlist_state s',query)
                if source == 'readable':
                    self.assertNotIn('FROM public.channels ',query)
                self.assertIn("s.video_type='long'",query)
    def test_historical_output_names_and_order_are_preserved(self):
        self.assertEqual(COLUMNS,RETIRED_VIEW_COLUMNS)
        self.assertEqual(len(COLUMNS),50)
    def test_no_active_db_string_references_a_retired_view(self):
        retired=RETIRED_VIEWS
        failures=[]
        for path in (ROOT/'application/db').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(node,ast.Constant) and isinstance(node.value,str):
                    for name in retired:
                        if re.search(r'\b'+re.escape(name)+r'\b',node.value):failures.append((path.name,node.lineno,name))
        self.assertEqual(failures,[])
    def test_maintenance_other_tables_and_rejected_identifiers_unchanged(self):
        for table in ('videos','video_inventory'):
            d=Recorded();InventoryMaintenanceRepository(d).fetch_all_rows(table,'video_id','video_id',page_size=3)
            self.assertIn('FROM "public"."'+table+'"',d.calls[0][1]);self.assertEqual(d.calls[0][2:],((3,0),3))
        for field in ('title;drop table channels','title desc','title,channel_id'):
            d=Recorded()
            with self.assertRaises(ValueError):InventoryMaintenanceRepository(d).load_channels_page('channel_id',offset=0,page_size=3,order_column=field)
            self.assertEqual(d.calls,[])

if __name__=='__main__':unittest.main()
