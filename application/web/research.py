"""Owner or scoped-agent research reads; field administration remains owner-only."""
import os

from fastapi import HTTPException, Request

from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import DatabaseError
from db.user_work.research_preferences import ResearchPreferencesRepository
from research_field_policy import FieldPolicyError, FIELDS as OPTIONAL_FIELDS, TYPES
from research_query.cursor import CursorCodec
from research_query.admission import ReadAdmission, LimitedReadAdmission
from research_query.frozen import FrozenStore
from research_query.http import create_router
from research_query.operations import OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected

# An operation runs only when every installation flag of its policy is on; an
# unknown policy never runs. Channel history belongs to the base analytics the
# installer enables. CHANNEL_TIMELINE_INSIGHTS_ENABLED enabled only the old channel Timeline
# route, removed: installations may still carry it, it enables nothing.
# The Inspector launch comparison requires all three policy flags.
POLICY_FLAGS = {
    "source_observation": (),
    "descriptive_analytics": ("CHANNEL_BASELINE_ANALYTICS_ENABLED",),
    "channel_history": ("CHANNEL_BASELINE_ANALYTICS_ENABLED",),
    "additional_analytics": ("CHANNEL_DERIVED_INSIGHTS_ENABLED",),
    "launch_analytics": ("CHANNEL_LAUNCH_ANALYTICS_ENABLED", "CHANNEL_BASELINE_ANALYTICS_ENABLED",
                         "CHANNEL_DERIVED_INSIGHTS_ENABLED"),
    # Channel header totals and 7D/28D changes have no analytics feature gate.
    "channel_header": (),
    # Home "Latest videos" relative view pace: never gated.
    "home_feed": (),
}


def flag(name):
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def allowed_operations():
    """Registered operations the installation flags allow; each flag is read once per request."""
    on = {name: flag(name) for names in POLICY_FLAGS.values() for name in names}
    return frozenset(op.id for op in OPERATIONS.values()
                     if op.policy in POLICY_FLAGS and all(on[name] for name in POLICY_FLAGS[op.policy]))


def create_research_router(database, frozen=None):
    # `frozen`: the process-owned frozen results, shared with the cache
    # invalidation hooks (web/cache.py); a private store when none is given.
    repository, codec = ResearchQueryRepository(database), CursorCodec()
    frozen = frozen if frozen is not None else FrozenStore()
    pool_size = getattr(getattr(database, 'config', None), 'max_size', 4)
    admission = ReadAdmission(concurrency=max(1, min(2, pool_size - 1)))
    # All agent keys/processes share this single lane, before the global queue.
    agent_admission = LimitedReadAdmission(admission, concurrency=1)

    # Catalogue access requires the owner, read admission and a prepared profile.
    # Calculated operations also require their analytics flags.

    def authorize_owner(request: Request):
        if (getattr(request.state, "installation_access", False) is not True
                or not callable(getattr(request.state, "owner_access_recheck", None))):
            raise HTTPException(401, detail="authentication_required")

    def authorize_research(request: Request):
        if (getattr(request.state, 'research_access', False) is True
                and callable(getattr(request.state, 'research_access_recheck', None))):
            return
        authorize_owner(request)

    def service_provider(request: Request):
        agent = getattr(request.state, "access_mode", None) == "agent"
        check = (request.state.research_access_recheck if getattr(request.state, 'research_access', False)
                 else request.state.owner_access_recheck)

        def context():
            revision = check()
            if revision is None:
                raise QueryRejected("policy_denied")
            try:
                preferences=ResearchPreferencesRepository(database).read()
            except FieldPolicyError:
                raise QueryRejected('backend_unavailable') from None
            disabled=frozenset((kind,field) for kind in TYPES for field in OPTIONAL_FIELDS
                               if preferences['effective'][kind][field]=='off')
            # The owner's config.ini formats, read live: Library,
            # the workshop, canvases, Saved and agents refuse a turned-off format.
            from collection_formats import disabled_formats, shown_formats
            return AccessContext(workspace_id="installation", access_revision=revision,
                                 disabled_video_types=frozenset(disabled_formats(shown_formats())),
                                 preferences_revision=str(preferences['revision']),disabled_type_fields=disabled,
                                 enabled=True, allow_frozen=not agent, allowed_operations=allowed_operations())

        def preparation_check():
            projection = getattr(request.app.state, 'research_projection', None)
            if projection is None:
                raise QueryRejected('backend_unavailable')
            if not projection.initial_fill_complete:
                if projection.preparation_failing:
                    raise QueryRejected('backend_unavailable')
                raise QueryRejected('catalogue_preparing')

        # A request owns its access context; the process owns key, admission
        # and frozen results.
        return QueryService(repository, context, codec=codec, admission=agent_admission if agent else admission,
                            preparation_check=preparation_check, frozen=frozen)

    from .research_fields import create_field_router
    from db.reads.research_sources import ResearchSourcesRepository
    sources = ResearchSourcesRepository(database)
    def source_search(kind, search, limit, after):
        try:
            return sources.search(kind, search, limit, after)
        except DatabaseError:
            raise QueryRejected('backend_unavailable') from None
    router=create_router(service_provider, authorize_research, source_search)
    router.include_router(create_field_router(database, authorize_owner))
    return router
