"""Explorer catalogue and analytic HTTP adapters."""
from catalogue_browse import CatalogueBrowseError, RecordingCatalogueRepository, normalize_browse_query
from catalogue_scope import CatalogueReadBudget, CatalogueScopeError
from db import (
    ChannelBaselineRepository,
    ChannelCounterDynamicsRepository,
    ChannelDerivedInsightsRepository,
    ChannelExplorerRepository,
    ChannelTimelineRepository,
    ChannelVideoGrowthRepository,
    FrontendRepository,
)
from channel_counter_dynamics import (
    ChannelCounterDynamicsNotFoundError,
    build_channel_counter_dynamics,
    build_channel_counter_dynamics_periods,
)
from channel_explorer import (
    ChannelExplorerNotFoundError,
    ChannelExplorerQueryError,
    build_channel_explorer,
    build_channel_format_totals,
    build_channel_profile,
)
from channel_timeline import (
    ChannelTimelineNotFoundError,
    ChannelTimelineQueryError,
    build_channel_timeline,
)
from fastapi import HTTPException, APIRouter
from starlette.responses import Response
from db.reads.channel_video_growth import VideoGrowthResponseError
from db.reads.channel_publications import ChannelPublicationsRepository
from channel_publications import PublicationReadError, build_channel_publications, VIDEO_ID
from video_history import VideoHistoryError, build_video_history
from video_launch import VideoLaunchError, build_video_launch
from channel_baseline import build_channel_baseline
from channel_derived_insights import build_channel_derived_insights
from catalogue_filters import build_channel_filter_facets, normalize_catalogue_filters
from channel_video_growth import build_channel_video_growth
from hot_storage import utc_instant as hot_instant, register_read_deadline
import re
from typing import Literal
from catalogue_static import requires_static_analysis
from datetime import timedelta
from .constants import EXPLORER_CAPABILITIES_CONTRACT_VERSION


CatalogueVideoType = Literal["long", "short", "stream"]

def create_explorer_router(*, cache, library, core, database, CHANNEL_BASELINE_ANALYTICS_ENABLED, CHANNEL_DERIVED_INSIGHTS_ENABLED, CHANNEL_LAUNCH_ANALYTICS_ENABLED, CHANNEL_TIMELINE_INSIGHTS_ENABLED):
    router = APIRouter()
    load_visible_channel_or_404 = library.load_visible_channel_or_404
    CATALOGUE_BROWSE_CACHE = cache.catalogue

    @router.get("/explorer/channel-library/video-channel")
    def explorer_channel_library_video_channel(video_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid YouTube video ID")

        repository = FrontendRepository(database)
        channel_id = repository.load_channel_id_for_video(video_id)
        channel_rows = repository.load_channel(channel_id) if channel_id else []
        visible_channel_id = (
            channel_id
            if channel_rows and not channel_rows[0].get("is_hidden")
            else None
        )
        return {
            "contract_version": 1,
            "status": "matched" if visible_channel_id else "not_tracked",
            "video_id": video_id,
            "channel_id": visible_channel_id,
            "source": {
                "kind": "stored_video_channel_mapping",
                "youtube_requests": False,
                "database_writes": False,
            },
        }


    def _explorer_capability_state(enabled):
        return "available" if enabled is True else "unavailable"


    @router.get("/explorer/capabilities")
    async def channel_explorer_capabilities(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {
            "contract_version": EXPLORER_CAPABILITIES_CONTRACT_VERSION,
            "catalogue_api": {
                "video_types": ["long", "short", "stream"],
                "one_type_per_request": True,
                "raw": "available",
                "filter_facets": _explorer_capability_state(CHANNEL_BASELINE_ANALYTICS_ENABLED),
                "static_video_details": _explorer_capability_state(CHANNEL_BASELINE_ANALYTICS_ENABLED),
                "video_growth": _explorer_capability_state(CHANNEL_DERIVED_INSIGHTS_ENABLED),
                "ui_video_types": ["long"],
            },
            "channel_explorer": {
                "raw": "available",
                "baseline": _explorer_capability_state(
                    CHANNEL_BASELINE_ANALYTICS_ENABLED
                ),
                "filter_facets": _explorer_capability_state(
                    CHANNEL_BASELINE_ANALYTICS_ENABLED
                ),
                "static_video_details": _explorer_capability_state(
                    CHANNEL_BASELINE_ANALYTICS_ENABLED
                ),
                "measured_change": _explorer_capability_state(
                    CHANNEL_DERIVED_INSIGHTS_ENABLED
                ),
                "video_growth": _explorer_capability_state(
                    CHANNEL_DERIVED_INSIGHTS_ENABLED
                ),
                "video_history": _explorer_capability_state(
                    CHANNEL_DERIVED_INSIGHTS_ENABLED
                ),
                "video_launch": _explorer_capability_state(
                    CHANNEL_LAUNCH_ANALYTICS_ENABLED is True
                    and CHANNEL_BASELINE_ANALYTICS_ENABLED is True
                    and CHANNEL_DERIVED_INSIGHTS_ENABLED is True
                ),
                "timeline": _explorer_capability_state(
                    CHANNEL_TIMELINE_INSIGHTS_ENABLED
                ),
            },
        }


    def _require_static_catalogue_gate(catalogue_filters):
        try:
            normalized = normalize_catalogue_filters(catalogue_filters, {})
        except ValueError as error:
            raise ChannelExplorerQueryError(str(error)) from error
        if requires_static_analysis(normalized) and CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Static catalogue analysis is not enabled")


    def _channel_explorer_video_catalogue(
        channel_id: str,
        *,
        video_type: str,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
        catalogue_filters: str | None = None,
    ):
        try:
            _require_static_catalogue_gate(catalogue_filters)
            return build_channel_explorer(
                ChannelExplorerRepository(database),
                channel_id,
                video_type=video_type,
                typed_catalogue_read=True,
                catalogue_filters=catalogue_filters,
                q=q,
                category_id=category_id,
                published_from=published_from,
                published_before=published_before,
                min_views=min_views,
                max_views=max_views,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/videos/long")
    @router.get("/channels/{channel_id}/explorer")
    def channel_explorer(
        channel_id: str,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
        catalogue_filters: str | None = None,
    ):
        return _channel_explorer_video_catalogue(
            channel_id,
            video_type=core.VIDEO_TYPE_LONG,
            catalogue_filters=catalogue_filters,
            q=q,
            category_id=category_id,
            published_from=published_from,
            published_before=published_before,
            min_views=min_views,
            max_views=max_views,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )


    @router.get("/channels/{channel_id}/explorer/videos/short")
    def channel_explorer_short_videos(
        channel_id: str,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
        catalogue_filters: str | None = None,
    ):
        return _channel_explorer_video_catalogue(
            channel_id,
            video_type=core.VIDEO_TYPE_SHORT,
            q=q,
            category_id=category_id,
            published_from=published_from,
            published_before=published_before,
            min_views=min_views,
            max_views=max_views,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
            catalogue_filters=catalogue_filters,
        )


    @router.get("/channels/{channel_id}/explorer/videos/stream")
    def channel_explorer_stream_videos(
        channel_id: str,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
        catalogue_filters: str | None = None,
    ):
        return _channel_explorer_video_catalogue(
            channel_id,
            video_type=core.VIDEO_TYPE_STREAM,
            q=q,
            category_id=category_id,
            published_from=published_from,
            published_before=published_before,
            min_views=min_views,
            max_views=max_views,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
            catalogue_filters=catalogue_filters,
        )


    def _run_catalogue_projection(builder, repository, channel_id, *, full_scope=False, **values):
        if not full_scope:
            return builder(repository, channel_id, **values)
        with CATALOGUE_BROWSE_CACHE.build_slot():
            return builder(repository, channel_id, full_scope=True, **values)


    def _catalogue_browse_response(channel_id, source, *, video_type="long", cursor=None, anchor_video_id=None, **values):
        if source == "growth" and CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Video growth is not enabled")
        try:
            _require_static_catalogue_gate(values.get("catalogue_filters"))
            # Cached pages never bypass current visibility or the derived gate.
            load_visible_channel_or_404(channel_id)
            query = normalize_browse_query(source, values)
            if cursor:
                payload = CATALOGUE_BROWSE_CACHE.resume(channel_id, source, query, cursor, video_type=video_type)
                register_read_deadline(hot_instant(payload["browse"]["expires_at"]))
                return payload
            if anchor_video_id is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{11}", anchor_video_id):
                raise HTTPException(status_code=422, detail="Invalid anchor video ID")
            values["catalogue_filters"] = values.get("catalogue_filters") or '{"version":1}'

            def build(now):
                source_valid_until = now + timedelta(seconds=CATALOGUE_BROWSE_CACHE.ttl_seconds)
                repository = RecordingCatalogueRepository(
                    ChannelVideoGrowthRepository(database) if source == "growth" else ChannelExplorerRepository(database),
                    video_type=video_type,
                    capture_growth=bool(query.get("catalogue_filters", {}).get("growth")),
                    now=now, source_valid_until=source_valid_until,
                )
                builder = build_channel_video_growth if source == "growth" else build_channel_explorer
                payload = builder(repository, channel_id, video_type=video_type, typed_catalogue_read=True,
                                  now=now, source_valid_until=source_valid_until, full_scope=True, all_rows=True, **values)
                return payload, repository.snapshot

            payload = CATALOGUE_BROWSE_CACHE.begin(channel_id, source, query, build, video_type=video_type, anchor_video_id=anchor_video_id)
            register_read_deadline(hot_instant(payload["browse"]["expires_at"]))
            return payload
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except (CatalogueScopeError, VideoGrowthResponseError) as error:
            raise HTTPException(status_code=503, detail="Catalogue read could not complete safely; retry the request") from error
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/videos/{video_type}/browse")
    def channel_explorer_catalogue_browse(
        channel_id: str, response: Response, video_type: CatalogueVideoType,
        q: str = "", category_id: str = "", published_from: str = "", published_before: str = "",
        min_views: int | None = None, max_views: int | None = None,
        min_duration_seconds: int | None = None, max_duration_seconds: int | None = None,
        sort_by: str = "published_at", sort_order: str = "desc", limit: int = 50, offset: int = 0,
        catalogue_filters: str | None = None, cursor: str | None = None, anchor_video_id: str | None = None,
    ):
        response.headers["Cache-Control"] = "no-store"
        return _catalogue_browse_response(
            channel_id, "raw", video_type=video_type, cursor=cursor, anchor_video_id=anchor_video_id,
            q=q, category_id=category_id, published_from=published_from, published_before=published_before,
            min_views=min_views, max_views=max_views, min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds, sort_by=sort_by, sort_order=sort_order,
            limit=limit, offset=offset, catalogue_filters=catalogue_filters,
        )


    @router.get("/channels/{channel_id}/explorer/videos/{video_type}/growth/browse")
    @router.get("/channels/{channel_id}/explorer/video-growth/browse")
    def channel_explorer_growth_browse(
        channel_id: str, response: Response, video_type: CatalogueVideoType = "long",
        q: str = "", category_id: str = "", published_from: str = "", published_before: str = "",
        min_views: int | None = None, max_views: int | None = None,
        min_duration_seconds: int | None = None, max_duration_seconds: int | None = None,
        sort_by: str = "published_at", sort_order: str = "desc", limit: int = 50, offset: int = 0,
        catalogue_filters: str | None = None, cursor: str | None = None, anchor_video_id: str | None = None,
    ):
        response.headers["Cache-Control"] = "no-store"
        return _catalogue_browse_response(
            channel_id, "growth", video_type=video_type, cursor=cursor, anchor_video_id=anchor_video_id,
            q=q, category_id=category_id, published_from=published_from, published_before=published_before,
            min_views=min_views, max_views=max_views, min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds, sort_by=sort_by, sort_order=sort_order,
            limit=limit, offset=offset, catalogue_filters=catalogue_filters,
        )


    @router.get("/channels/{channel_id}/explorer/videos/{video_type}/growth")
    @router.get("/channels/{channel_id}/explorer/video-growth")
    def channel_explorer_video_growth(
        channel_id: str,
        video_type: CatalogueVideoType = "long",
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
        catalogue_filters: str | None = None,
    ):
        if CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Video growth is not enabled")
        try:
            _require_static_catalogue_gate(catalogue_filters)
            return build_channel_video_growth(
                ChannelVideoGrowthRepository(database),
                channel_id,
                video_type=video_type,
                typed_catalogue_read=True,
                catalogue_filters=catalogue_filters,
                q=q,
                category_id=category_id,
                published_from=published_from,
                published_before=published_before,
                min_views=min_views,
                max_views=max_views,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (VideoGrowthResponseError, CatalogueScopeError) as error:
            raise HTTPException(
                status_code=503,
                detail="Video growth results are incomplete or invalid; retry the request",
            ) from error


    @router.get("/channels/{channel_id}/explorer/videos/{video_type}/{video_id}/static-details")
    def channel_explorer_video_static_details(channel_id: str, video_id: str, response: Response, browse_session: str, video_type: CatalogueVideoType):
        response.headers["Cache-Control"] = "no-store"
        if CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Static video details are not enabled")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid video ID")
        try:
            load_visible_channel_or_404(channel_id)
            with CATALOGUE_BROWSE_CACHE.build_slot():
                return CATALOGUE_BROWSE_CACHE.video_static_details(
                    browse_session, channel_id, video_id, video_type=video_type,
                    growth_enabled=CHANNEL_DERIVED_INSIGHTS_ENABLED is True,
                )
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error


    @router.get("/channels/{channel_id}/explorer/videos/long/{video_id}/lifetime-baseline")
    def channel_explorer_video_lifetime_baseline(channel_id: str, video_id: str, response: Response, browse_session: str):
        response.headers["Cache-Control"] = "no-store"
        if CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Channel baseline is not enabled")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid video ID")
        try:
            load_visible_channel_or_404(channel_id)
            with CATALOGUE_BROWSE_CACHE.build_slot():
                result = CATALOGUE_BROWSE_CACHE.video_lifetime_baseline(
                    browse_session, channel_id, video_id,
                    growth_enabled=CHANNEL_DERIVED_INSIGHTS_ENABLED is True,
                )
                if CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
                    raise HTTPException(status_code=404, detail="Channel baseline is not enabled")
                load_visible_channel_or_404(channel_id)
                CATALOGUE_BROWSE_CACHE.validate_inspector_context(
                    browse_session, channel_id,
                    growth_enabled=CHANNEL_DERIVED_INSIGHTS_ENABLED is True,
                )
                return result
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail="Channel not found") from error
        except CatalogueScopeError as error:
            raise HTTPException(status_code=503, detail="Channel baseline could not complete safely; retry baseline") from error


    @router.get("/channels/{channel_id}/explorer/videos/long/{video_id}/launch")
    def channel_explorer_video_launch(channel_id: str, video_id: str, response: Response, browse_session: str):
        response.headers["Cache-Control"] = "no-store"
        if not (CHANNEL_LAUNCH_ANALYTICS_ENABLED is True
                and CHANNEL_BASELINE_ANALYTICS_ENABLED is True
                and CHANNEL_DERIVED_INSIGHTS_ENABLED is True):
            raise HTTPException(status_code=404, detail="Launch comparison is not enabled")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid video ID")
        try:
            load_visible_channel_or_404(channel_id)
            with CATALOGUE_BROWSE_CACHE.build_slot():
                budget = CatalogueReadBudget()
                context = CATALOGUE_BROWSE_CACHE.video_launch_context(
                    browse_session, channel_id, video_id, growth_enabled=True, read_budget=budget,
                )
                result = build_video_launch(ChannelDerivedInsightsRepository(database), context,
                                            now=CATALOGUE_BROWSE_CACHE.clock(), read_budget=budget)
                if not (CHANNEL_LAUNCH_ANALYTICS_ENABLED is True
                        and CHANNEL_BASELINE_ANALYTICS_ENABLED is True
                        and CHANNEL_DERIVED_INSIGHTS_ENABLED is True):
                    raise HTTPException(status_code=404, detail="Launch comparison is not enabled")
                load_visible_channel_or_404(channel_id)
                CATALOGUE_BROWSE_CACHE.validate_inspector_context(
                    browse_session, channel_id, growth_enabled=True,
                )
                return result
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail="Channel not found") from error
        except (VideoLaunchError, CatalogueScopeError) as error:
            raise HTTPException(status_code=503, detail="Launch comparison could not complete safely; retry comparison") from error


    @router.get("/channels/{channel_id}/explorer/videos/{video_type}/filter-facets")
    def channel_explorer_filter_facets(
        channel_id: str,
        response: Response,
        video_type: CatalogueVideoType,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        catalogue_filters: str | None = None,
        full_catalogue: bool = False,
        browse_session: str | None = None,
    ):
        response.headers["Cache-Control"] = "no-store"
        if CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Catalogue filter facets are not enabled")
        try:
            values = dict(q=q, category_id=category_id, published_from=published_from,
                          published_before=published_before, min_views=min_views, max_views=max_views,
                          min_duration_seconds=min_duration_seconds, max_duration_seconds=max_duration_seconds,
                          catalogue_filters=catalogue_filters)
            query = normalize_browse_query("growth", values)
            dynamic_filters = bool(query.get("catalogue_filters", {}).get("growth"))
            if dynamic_filters:
                if CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
                    raise HTTPException(status_code=404, detail="Video growth is not enabled")
                if not browse_session:
                    raise HTTPException(status_code=422, detail="Growth conditions require a matching growth browse session")
            if browse_session:
                if full_catalogue is not True:
                    raise HTTPException(status_code=422, detail="browse_session requires full_catalogue")
                load_visible_channel_or_404(channel_id)
                # Admission precedes decoding/indexing the potentially large snapshot.
                with CATALOGUE_BROWSE_CACHE.build_slot():
                    repository, now = CATALOGUE_BROWSE_CACHE.facet_source(
                        browse_session, channel_id, query, video_type=video_type,
                        growth_enabled=CHANNEL_DERIVED_INSIGHTS_ENABLED is True,
                        require_growth=dynamic_filters,
                    )
                    result = build_channel_filter_facets(repository, channel_id, video_type=video_type, typed_catalogue_read=True, full_scope=True, frozen_growth=dynamic_filters, now=now, **values)
                    # Frozen observations can expire while the full catalogue is projected.
                    CATALOGUE_BROWSE_CACHE.validate_inspector_context(
                        browse_session, channel_id, video_type=video_type,
                        growth_enabled=CHANNEL_DERIVED_INSIGHTS_ENABLED is True,
                    )
                    return result
            return _run_catalogue_projection(build_channel_filter_facets, ChannelExplorerRepository(database),
                                             channel_id, video_type=video_type, typed_catalogue_read=True, full_scope=full_catalogue is True, **values)
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except CatalogueScopeError as error:
            raise HTTPException(status_code=503, detail="Catalogue read could not complete safely; retry the request") from error


    @router.get("/channels/{channel_id}/explorer/videos/long/{video_id}/history")
    def channel_explorer_video_history(channel_id: str, video_id: str, response: Response, browse_session: str):
        response.headers["Cache-Control"] = "no-store"
        if CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Video history is not enabled")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid video ID")
        try:
            load_visible_channel_or_404(channel_id)
            with CATALOGUE_BROWSE_CACHE.build_slot():
                context = CATALOGUE_BROWSE_CACHE.video_history_context(
                    browse_session, channel_id, video_id, growth_enabled=True,
                )
                result = build_video_history(ChannelDerivedInsightsRepository(database), context,
                                             now=CATALOGUE_BROWSE_CACHE.clock())
                if CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
                    raise HTTPException(status_code=404, detail="Video history is not enabled")
                load_visible_channel_or_404(channel_id)
                CATALOGUE_BROWSE_CACHE.validate_video_history_context(browse_session, channel_id)
                return result
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except (VideoHistoryError, CatalogueScopeError) as error:
            raise HTTPException(status_code=503, detail="Video history could not complete safely; retry history") from error


    @router.get("/channels/{channel_id}/explorer/publications")
    def channel_explorer_publications(channel_id: str, response: Response, cursor: str | None = None):
        response.headers["Cache-Control"] = "no-store"
        if cursor is not None and not VIDEO_ID.fullmatch(cursor):
            raise HTTPException(status_code=422, detail="Invalid publication page cursor")
        load_visible_channel_or_404(channel_id)
        try:
            return build_channel_publications(ChannelPublicationsRepository(database), channel_id, cursor=cursor)
        except PublicationReadError as error:
            raise HTTPException(status_code=503, detail="Publication status could not be read safely; retry") from error


    @router.get("/channels/{channel_id}/explorer/profile")
    def channel_explorer_profile(channel_id: str):
        try:
            return build_channel_profile(
                ChannelExplorerRepository(database),
                channel_id,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/format-totals")
    def channel_explorer_format_totals(channel_id: str, include_typed: bool = False):
        try:
            return build_channel_format_totals(
                ChannelExplorerRepository(database),
                channel_id,
                **({"include_typed": True} if include_typed is True else {}),
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/counter-dynamics-28d")
    def channel_explorer_counter_dynamics_28d(channel_id: str):
        try:
            return build_channel_counter_dynamics(
                ChannelCounterDynamicsRepository(database),
                channel_id,
            )
        except ChannelCounterDynamicsNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/counter-dynamics")
    def channel_explorer_counter_dynamics_periods(channel_id: str):
        try:
            return build_channel_counter_dynamics_periods(
                ChannelCounterDynamicsRepository(database),
                channel_id,
            )
        except ChannelCounterDynamicsNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/timeline")
    def channel_explorer_timeline(
        channel_id: str,
        metric: str = "views",
        period: str = "28d",
    ):
        if CHANNEL_TIMELINE_INSIGHTS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Channel Timeline is not enabled")
        try:
            return build_channel_timeline(
                ChannelTimelineRepository(database),
                channel_id,
                metric=metric,
                period=period,
            )
        except ChannelTimelineNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelTimelineQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


    @router.get("/channels/{channel_id}/explorer/videos/long/baseline")
    @router.get("/channels/{channel_id}/explorer/baseline")
    def channel_explorer_baseline(
        channel_id: str,
        period: str = "max",
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        catalogue_filters: str | None = None,
        full_catalogue: bool = False,
    ):
        if CHANNEL_BASELINE_ANALYTICS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Baseline Analytics is not enabled")
        try:
            return _run_catalogue_projection(
                build_channel_baseline,
                ChannelBaselineRepository(database),
                channel_id,
                full_scope=full_catalogue is True,
                catalogue_filters=catalogue_filters,
                period=period,
                q=q,
                category_id=category_id,
                published_from=published_from,
                published_before=published_before,
                min_views=min_views,
                max_views=max_views,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except CatalogueBrowseError as error:
            raise HTTPException(status_code=error.status_code, detail=error.code) from error
        except CatalogueScopeError as error:
            raise HTTPException(status_code=503, detail="Catalogue read could not complete safely; retry the request") from error


    @router.get("/channels/{channel_id}/explorer/videos/long/derived-insights")
    @router.get("/channels/{channel_id}/explorer/derived-insights")
    def channel_explorer_derived_insights(
        channel_id: str,
        q: str = "",
        category_id: str = "",
        published_from: str = "",
        published_before: str = "",
        min_views: int | None = None,
        max_views: int | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        catalogue_filters: str | None = None,
    ):
        if CHANNEL_DERIVED_INSIGHTS_ENABLED is not True:
            raise HTTPException(status_code=404, detail="Derived Insights is not enabled")
        try:
            return build_channel_derived_insights(
                ChannelDerivedInsightsRepository(database),
                channel_id,
                q=q,
                category_id=category_id,
                published_from=published_from,
                published_before=published_before,
                min_views=min_views,
                max_views=max_views,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                catalogue_filters=catalogue_filters,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChannelExplorerQueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


    return router
