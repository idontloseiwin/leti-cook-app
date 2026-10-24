"""Route table after removing unused legacy routes. Reads source only: no server, database or provider."""
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
        create_explorer_router(cache=cache, library=library, core=core, database=None,
            CHANNEL_BASELINE_ANALYTICS_ENABLED=False, CHANNEL_DERIVED_INSIGHTS_ENABLED=False,
            CHANNEL_LAUNCH_ANALYTICS_ENABLED=False, CHANNEL_TIMELINE_INSIGHTS_ENABLED=False),
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
# Called by the V3 interface; the job mechanism stays behind /hydration.
KEPT={
    ('GET','/system/run-history'),
    ('POST','/add-channel'),('GET','/sidebar-data'),('GET','/system-summary'),('GET','/system/pipeline-health'),
    ('GET','/explorer/capabilities'),('GET','/explorer/home-summary'),('GET','/explorer/recent-videos'),
    ('GET','/explorer/channel-library/video-channel'),('GET','/channels/{channel_id}/explorer'),
    ('GET','/channels/{channel_id}/hydration'),('POST','/channels/{channel_id}/hydration'),
    ('POST','/folders'),('POST','/labels'),('POST','/channels/{channel_id}/folder'),
    ('PATCH','/folders/{folder_id}'),('DELETE','/folders/{folder_id}'),('PATCH','/labels/{label_key}'),('DELETE','/labels/{label_key}'),
    ('POST','/channels/{channel_id}/labels/{label_key}'),('GET','/app-explorer-v3'),
}
REMOVED_FRAGMENTS=('stats-health','analytics-summary','analytics/long/','period-analytics','videos-summary',
                   'videos/long/summary','api-quota','channel-coverage','sync-runtime','set-deep-bypass',
                   'remove-channel','initial-long-inventory')

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
