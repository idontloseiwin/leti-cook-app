"""Provider text beyond the stored bounds fails its own item, before the database refuses it (audit S5-12). Source only."""
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import re
import sys
import unittest


ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from collection import video_payloads, channel_metadata

CHANNEL_METADATA=ROOT/'application/collection/channel_metadata.py'
SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8')
BOUNDS={'VIDEO_TITLE_MAX_CHARS','VIDEO_TAGS_MAX_COUNT','VIDEO_TAGS_MAX_BYTES','CHANNEL_TITLE_MAX_CHARS',
        'CHANNEL_AVATAR_URL_MAX_CHARS'}

def functions():
    functions = {'build_video_collection_metadata_payload': partial(
        video_payloads.build_video_collection_metadata_payload,
        batch_stats_enabled=False, parse_dt=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')),
        view_count_absent_error=RuntimeError, view_count_invalid_error=RuntimeError),
        'channel_metadata_update_values': channel_metadata.channel_metadata_update_values}
    functions.update({name: getattr(video_payloads if name.startswith('VIDEO_') else channel_metadata, name)
                      for name in BOUNDS})
    return functions


def video(title='t',tags=None):
    snippet={'title':title,'publishedAt':'2026-09-26T00:00:00Z'}
    if tags is not None:snippet['tags']=tags
    return {'id':'vid00000001','snippet':snippet,'statistics':{'viewCount':'1'}}

class VideoTextTests(unittest.TestCase):
    def test_values_at_the_bound_are_accepted(self):
        build=functions()['build_video_collection_metadata_payload']
        row=build('UC1',video('t'*1000,['x']*500))['row']
        self.assertEqual((row['title'],row['tags']),('t'*1000,['x']*500))
        self.assertEqual(build('UC1',video(tags=['x'*25600]))['row']['tags'],['x'*25600])
        utf8_tag='é'*12800
        self.assertEqual(len(utf8_tag.encode('utf-8')),25600)
        self.assertEqual(build('UC1',video(tags=[utf8_tag]))['row']['tags'],[utf8_tag])

    def test_values_beyond_it_fail_that_video(self):
        build=functions()['build_video_collection_metadata_payload']
        for item in (video('t'*1001),video(tags=['x']*501),video(tags=['x'*25601]),video(tags=['é'*12801])):
            with self.assertRaises(ValueError):build('UC1',item)

class ChannelTextTests(unittest.TestCase):
    def item(self,title='Channel',avatar='https://yt3.ggpht.com/a'):
        return {'snippet':{'title':title,'thumbnails':{'default':{'url':avatar}}}}

    def test_a_long_title_fails_the_refresh(self):
        values=functions()['channel_metadata_update_values']
        self.assertEqual(values(self.item('c'*1000),datetime.now(timezone.utc))['title'],'c'*1000)
        with self.assertRaises(ValueError):values(self.item('c'*1001),datetime.now(timezone.utc))

    def test_a_long_avatar_is_dropped(self):
        values=functions()['channel_metadata_update_values']
        self.assertEqual(values(self.item(avatar='a'*2048),datetime.now(timezone.utc))['avatar_url'],'a'*2048)
        self.assertIsNone(values(self.item(avatar='a'*2049),datetime.now(timezone.utc))['avatar_url'])

class SchemaAgreementTests(unittest.TestCase):
    def constraint(self,name):
        match=re.search(r'CONSTRAINT '+name+r' CHECK \((.*)\),?\r?\n',SCHEMA)
        self.assertIsNotNone(match,name)
        return match.group(1)

    def test_the_schema_bounds_match(self):
        bound=functions()
        self.assertIn(f"char_length(title) <= {bound['VIDEO_TITLE_MAX_CHARS']}",self.constraint('videos_title_bound_check'))
        tags=self.constraint('videos_tags_bound_check')
        self.assertIn(f"cardinality(tags) <= {bound['VIDEO_TAGS_MAX_COUNT']}",tags)
        self.assertIn(f"<= {bound['VIDEO_TAGS_MAX_BYTES']}",tags)
        self.assertIn('char_length(duration) <= 64',self.constraint('videos_duration_bound_check'))
        self.assertIn(f"char_length(title) <= {bound['CHANNEL_TITLE_MAX_CHARS']}",self.constraint('channels_title_bound_check'))
        self.assertIn(f"char_length(avatar_url) <= {bound['CHANNEL_AVATAR_URL_MAX_CHARS']}",
                      self.constraint('channels_avatar_url_bound_check'))

if __name__=='__main__':unittest.main()
