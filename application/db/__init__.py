"""Backend-owned database access boundary.

All application SQL construction belongs here.
Runtime modules pass a process-owned PostgresDatabase into named repositories;
they do not construct queries or manage individual connections themselves.
The retained DatabaseGateway name is an internal direct-transport type alias.
"""

from .videos.availability import (
    AvailabilityRepository,
)
from .base.client import (
    DatabaseGateway,
    create_database_gateway,
    fresh_database_gateway,
)
from .channel.channel_discovery import ChannelDiscoveryRepository
from .reads.channel_baseline import ChannelBaselineRepository
from .reads.channel_counter_dynamics import ChannelCounterDynamicsRepository
from .reads.channel_derived_insights import ChannelDerivedInsightsRepository
from .reads.channel_explorer import ChannelExplorerRepository
from .reads.channel_video_growth import ChannelVideoGrowthRepository
from .reads.channel_timeline import ChannelTimelineRepository
from .channel.channel_sync import (
    ChannelMetadataColumnsUnavailableError,
    ChannelSyncRepository,
)
from .user_work.channel_organization import ChannelOrganizationRepository
from .reads.frontend import FrontendRepository
from .inventory.inventory_maintenance import InventoryMaintenanceRepository
from .jobs.operator_jobs import OperatorJobRepository
from .base.repository import DatabaseRepository
from .reads.system_health import SystemHealthRepository
from .runs.sync_runs import SyncRunRepository
from .videos.video_collection import VideoCollectionClaim, VideoCollectionRepository
from .quota.youtube_quota import YouTubeQuotaRepository
from .inventory.typed_inventory import TypedInventoryClaim, TypedInventoryRepository

__all__ = (
    "AvailabilityRepository",
    "ChannelDiscoveryRepository",
    "ChannelBaselineRepository",
    "ChannelCounterDynamicsRepository",
    "ChannelDerivedInsightsRepository",
    "ChannelExplorerRepository",
    "ChannelVideoGrowthRepository",
    "ChannelTimelineRepository",
    "ChannelMetadataColumnsUnavailableError",
    "ChannelOrganizationRepository",
    "ChannelSyncRepository",
    "DatabaseGateway",
    "DatabaseRepository",
    "FrontendRepository",
    "InventoryMaintenanceRepository",
    "OperatorJobRepository",
    "SyncRunRepository",
    "SystemHealthRepository",
    "VideoCollectionClaim",
    "VideoCollectionRepository",
    "YouTubeQuotaRepository",
    "TypedInventoryClaim",
    "TypedInventoryRepository",
    "create_database_gateway",
    "fresh_database_gateway",
)
