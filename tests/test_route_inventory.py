"""The route table and the consumer of every route. Reads source only: no server, database or provider."""
from pathlib import Path
import re
import unittest
from http_test_support import ROOT, cache_stub, library_stub, channel_http, system_http
from types import SimpleNamespace
from web.library import create_library_http
from web.explorer import create_explorer_router
from web.home import create_home_router
from web.organization import create_organization_router
from web.frontend import create_frontend_router
SERVER=ROOT/'application/server.py'

def routes():
    """Inspect real APIRouter registrations without invoking a handler or lifespan."""
    cache, library, core = cache_stub(), library_stub(), SimpleNamespace()
    channels = channel_http()
    routers = [channels.add_router, create_library_http(cache=cache, database=None).router,
        create_explorer_router(database=None),
        channels.backfill_router, create_home_router(cache=cache, library=library, core=core, database=None),
        system_http(), channels.hydration_router,
        create_organization_router(cache=cache, library=library, database=None),
        create_frontend_router(FRONTEND_DIST_DIR=Path('unused-dist'))]
    result = []
    for router in routers:
        for route in router.routes:
            result.extend((method, route.path) for method in sorted(route.methods))
    return result

# No consumer in the V3 interface, setup/access pages, installer, manage.py, collector, scripts or tests.
REMOVED={
    ('GET','/channels/{channel_id}/videos/long/stats-health'),('GET','/channels/{channel_id}/stats-health'),
    ('GET','/channels/{channel_id}/analytics/long/summary'),('GET','/channels/{channel_id}/analytics-summary'),
    ('GET','/channels/{channel_id}/analytics/long/period'),('GET','/channels/{channel_id}/period-analytics'),
    ('GET','/channels/{channel_id}/videos/long/summary'),('GET','/channels/{channel_id}/videos-summary'),
    ('GET','/videos/long/{video_id}/analytics'),('GET','/video/{video_id}/analytics'),
    ('GET','/home-summary'),('GET','/api-quota'),('GET','/channel-coverage'),('GET','/system/sync-runtime'),
    ('POST','/set-deep-bypass'),('DELETE','/remove-channel/{channel_id}'),
    ('GET','/channels/{channel_id}/initial-long-inventory-job'),('POST','/channels/{channel_id}/initial-long-inventory-job'),
    ('POST','/operator-jobs/{job_id}/retry-initial-long-inventory'),('POST','/channels/{channel_id}/run-initial-long-inventory'),
    ('POST','/channels/{channel_id}/parent'),
}
# The old Library catalogue and analytics routes; the shared
# backend (/research/*) replaced them and the owner has no scripts that call them.
REMOVED|={('GET',route) for route in (
    '/explorer/capabilities','/explorer/recent-videos','/channels/{channel_id}/explorer',
    '/channels/{channel_id}/explorer/videos/long','/channels/{channel_id}/explorer/videos/short',
    '/channels/{channel_id}/explorer/videos/stream','/channels/{channel_id}/explorer/videos/{video_type}/browse',
    '/channels/{channel_id}/explorer/videos/{video_type}/growth/browse','/channels/{channel_id}/explorer/video-growth/browse',
    '/channels/{channel_id}/explorer/videos/{video_type}/growth','/channels/{channel_id}/explorer/video-growth',
    '/channels/{channel_id}/explorer/videos/{video_type}/{video_id}/static-details',
    '/channels/{channel_id}/explorer/videos/long/{video_id}/lifetime-baseline',
    '/channels/{channel_id}/explorer/videos/long/{video_id}/launch',
    '/channels/{channel_id}/explorer/videos/long/{video_id}/history',
    '/channels/{channel_id}/explorer/videos/{video_type}/filter-facets',
    '/channels/{channel_id}/explorer/counter-dynamics','/channels/{channel_id}/explorer/counter-dynamics-28d',
    '/channels/{channel_id}/explorer/timeline','/channels/{channel_id}/explorer/baseline',
    '/channels/{channel_id}/explorer/videos/long/baseline','/channels/{channel_id}/explorer/derived-insights',
    '/channels/{channel_id}/explorer/videos/long/derived-insights','/channels/{channel_id}/explorer/publications',
    '/channels/{channel_id}/explorer/format-totals')}
# Called by the V3 interface; the job mechanism stays behind /hydration.
KEPT={
    ('GET','/system/run-history'),
    ('POST','/add-channel'),('GET','/sidebar-data'),('GET','/system-summary'),('GET','/system/pipeline-health'),
    ('GET','/explorer/home-summary'),('GET','/explorer/channel-library/video-channel'),
    ('GET','/channels/{channel_id}/explorer/profile'),('GET','/channels/{channel_id}/explorer/list-inventory'),
    ('GET','/channels/{channel_id}/hydration'),('POST','/channels/{channel_id}/hydration'),
    ('POST','/folders'),('POST','/labels'),('POST','/channels/{channel_id}/folder'),
    ('PATCH','/folders/{folder_id}'),('DELETE','/folders/{folder_id}'),('PATCH','/labels/{label_key}'),('DELETE','/labels/{label_key}'),
    ('POST','/channels/{channel_id}/labels/{label_key}'),('GET','/app-explorer-v3'),
}
REMOVED_FRAGMENTS=('stats-health','analytics-summary','analytics/long/','period-analytics','videos-summary',
                   'videos/long/summary','api-quota','channel-coverage','sync-runtime','set-deep-bypass',
                   'remove-channel','initial-long-inventory',
                   'explorer/capabilities','recent-videos','format-totals','explorer/videos/','video-growth',
                   'counter-dynamics','derived-insights','explorer/timeline','explorer/baseline',
                   'explorer/publications','filter-facets','static-details','lifetime-baseline')

class RouteInventoryTests(unittest.TestCase):
    def test_unused_routes_are_removed_and_used_routes_remain(self):
        table=set(routes())
        self.assertEqual(REMOVED&table,set())
        self.assertEqual(KEPT-table,set())

    def test_frontend_assets_precede_the_spa_catch_all(self):
        ordered = routes()
        self.assertEqual(len(ordered), len(set(ordered)))
        self.assertLess(ordered.index(('GET', '/app-explorer-v3/assets/{asset_path:path}')),
                        ordered.index(('GET', '/app-explorer-v3/{path:path}')))

    def test_shipped_pages_call_no_removed_route(self):
        pages=[*(ROOT/'application/frontend/dist-self-hosted').rglob('*.js'),*(ROOT/'application/frontend/dist-self-hosted').glob('*.html'),
               *(ROOT/'application/self_hosted_ui').glob('*.*')]
        self.assertTrue(pages)
        for page in pages:
            text=page.read_text(encoding='utf-8',errors='replace')
            for fragment in REMOVED_FRAGMENTS:
                self.assertNotIn(fragment,text,f'{page.name} calls {fragment}')
            self.assertIsNone(re.search(r'(?<!explorer)/home-summary',text),page.name)

    def test_removed_diagnostics_leave_no_misleading_output(self):
        source='\n'.join(path.read_text(encoding='utf-8') for path in [SERVER, *(ROOT/'application/web').glob('*.py')])
        # The hard-coded 30-minute systemd schedule no longer appears in /system-summary.
        self.assertNotIn('next_auto_sync',source)
        self.assertNotIn('systemd timer',source)
        self.assertFalse((ROOT/'application/system_runtime_commands.py').exists())
        self.assertNotIn('InventoryMaintenanceRepository(',(ROOT/'application/core.py').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
