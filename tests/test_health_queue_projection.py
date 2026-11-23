"""Health must consume control fields and preserve absence/error distinctions."""
from datetime import datetime,timezone
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.reads.system_health import CHANNEL_HEALTH_COLUMNS
from youtube_pipeline_health import aggregate_channels,aggregate_channel_discovery_queue,aggregate_video_collection_queue

class HealthProjectionTests(unittest.TestCase):
 def test_selected_channel_facts_keep_active_hidden_and_held_distinct(self):
  now=datetime.now(timezone.utc)
  active=dict(channel_id='active',is_hidden=False,collection_permission='accepted',collection_control_epoch=1,
   collection_mode='active',channel_metadata_status='error',channel_metadata_next_after=now.isoformat())
  rows=[active,{**active,'channel_id':'hidden','is_hidden':True,'collection_mode':'hidden'},
   {**active,'channel_id':'held','collection_permission':'manual_required','collection_mode':'operator_hold'}]
  from youtube_pipeline_health import CHANNEL_COLUMNS
  self.assertEqual(CHANNEL_COLUMNS, CHANNEL_HEALTH_COLUMNS)
  selected=[{k:r.get(k) for k in CHANNEL_COLUMNS.split(',')} for r in rows]
  result=aggregate_channels(selected,now)
  self.assertEqual(result['sync_control']['mode_counts'],{'active':1,'hidden':1,'operator_hold':1})
  self.assertEqual(result['channel_metadata']['due'],1)
  self.assertEqual(result['channel_metadata']['status_counts'],{'error':1})
 def test_queue_projection_keeps_absence_and_policy_exclusions(self):
  now=datetime.now(timezone.utc)
  discovery=aggregate_channel_discovery_queue({'failed':2,'playlist_not_found':134},now)
  self.assertEqual((discovery['failed'],discovery['playlist_not_found']),(2,134))
  stats=aggregate_video_collection_queue([{'due':12,'paused':468,'policy_paused':468}],now)
  self.assertEqual((stats['all_due'],stats['policy_paused']),(12,468))

if __name__=='__main__':unittest.main()
