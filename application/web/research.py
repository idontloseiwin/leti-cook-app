"""Opt-in owner-only catalogue preview; binds the existing query executor."""
import os

from fastapi import HTTPException, Request

from db.reads.research_query import ResearchQueryRepository
from research_query.cursor import CursorCodec
from research_query.http import create_router
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected


def create_research_router(database):
    repository, codec = ResearchQueryRepository(database), CursorCodec()

    def enabled():
        # Experimental deployment switch, not a persisted user preference.
        return os.getenv("LIC_RESEARCH_QUERY_PREVIEW", "0") == "1"

    def authorize_owner(request: Request):
        if (getattr(request.state, "installation_access", False) is not True
                or not callable(getattr(request.state, "owner_access_recheck", None))):
            raise HTTPException(401, detail="authentication_required")

    def service_provider(request: Request):
        check = request.state.owner_access_recheck

        def context():
            revision = check()
            if revision is None:
                raise QueryRejected("policy_denied")
            return AccessContext(workspace_id="installation", access_revision=revision,
                                 enabled=enabled())

        # A request owns its access context; the process owns the cursor key.
        return QueryService(repository, context, codec=codec)

    return create_router(service_provider, authorize_owner)
