"""Owner-only local rules and inbox. No acquisition or collector dependency."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from alerts import AlertRule, COUNTERS, capabilities
from collection_formats import shown_formats
from db.user_work.alerts import AlertsRepository, Conflict
from hot_storage import register_read_deadline, utc_instant
from research_query.registry import SOURCE_ID, CHANNEL_SOURCE_ID, POSTGRES_BIGINT_MAX
from research_query.validation import QueryRejected, canonical


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    rule: AlertRule


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    rule: AlertRule | None = None
    enabled: bool | None = None


def validate_format(rule):
    if rule.video_type and rule.video_type not in shown_formats():
        raise HTTPException(409, detail="format_turned_off")


def preview_query(rule):
    channel = rule.event == "channel.subscribers"
    identity = "channel.id" if channel else "video.id"
    fields = [identity, "channel.title" if channel else "video.title"]
    if rule.event in COUNTERS:
        fields.append(rule.event)
    scope = rule.scope.model_dump()
    predicates = []
    if scope["kind"] == "video":
        predicates.append({"field":"video.id","op":"eq","value":scope["id"]})
        scope = {"kind":"library"}
    for value, op in ((rule.min_duration,"gte"), (rule.max_duration,"lte")):
        if value is not None:
            predicates.append({"field":"video.duration_seconds","op":op,"value":str(value)})
    if rule.event in COUNTERS:
        predicates.append({"field": rule.event, "op": "gte" if rule.direction == "up" else "lte",
                           "value": rule.threshold})
    query = {"query_version":1, "source":{"id":CHANNEL_SOURCE_ID if channel else SOURCE_ID},
             "scope":scope, "select":fields,"order_by":[{"field":identity,"direction":"asc"}],
             "consistency":"current","page":{"size":5}}
    if predicates:
        query["where"] = predicates[0] if len(predicates)==1 else {"all":predicates}
    if not channel:
        query["video_types"] = [rule.video_type]
    return query


def create_alert_router(database, service_provider, authorize_owner):
    repo = AlertsRepository(database)
    router = APIRouter(prefix="/alerts", dependencies=[Depends(authorize_owner)])

    def command(fn, *args):
        try:
            return fn(*args)
        except Conflict as error:
            raise HTTPException(409, detail=str(error)) from None

    @router.get("/capabilities")
    def describe():
        return {**capabilities(), "formats":[kind for kind in shown_formats() if kind in ("long","short")]}

    @router.post("/preview")
    def preview(rule: AlertRule, request: Request):
        validate_format(rule)
        service = service_provider(request)
        try:
            result = service.execute(canonical(preview_query(rule)))
            register_read_deadline(utc_instant(result["context"]["valid_before"]))
            return result
        except QueryRejected as error:
            return JSONResponse(error.payload(), status_code=error.status, headers={"Cache-Control":"no-store"})

    @router.get("/rules")
    def rules(request: Request):
        worker = getattr(request.app.state,"alert_worker",None)
        items = repo.rules()
        waiting = not worker or worker.failure or any(item["retry_pending"] for item in items)
        return {"items":items, "processor":"waiting" if waiting else "ready"}

    @router.post("/rules",status_code=201)
    def create(body: Create):
        validate_format(body.rule)
        return command(repo.create,body.id,body.rule)

    @router.patch("/rules/{rule_id}")
    def update(rule_id: UUID, body: Change):
        if body.rule:
            validate_format(body.rule)
        return command(repo.change,rule_id,body.version,body.rule,body.enabled)

    @router.delete("/rules/{rule_id}")
    def delete(rule_id: UUID, version: int = Query(ge=1)):
        command(repo.delete,rule_id,version)
        return {"deleted":True}

    @router.get("/inbox")
    def inbox(before: int = Query(default=POSTGRES_BIGINT_MAX,ge=1,le=POSTGRES_BIGINT_MAX)):
        items = repo.inbox(before,shown_formats())
        page = items[:50]
        if page:
            register_read_deadline(min(utc_instant(i["source_expires_at"]) for i in page))
        return {"items":page,"next":page[-1]["id"] if len(items)>50 else None}

    @router.post("/inbox/{item_id}/read")
    def read(item_id: int):
        if not 0 < item_id <= POSTGRES_BIGINT_MAX or not repo.read(item_id,shown_formats()):
            raise HTTPException(404,detail="alert_unavailable")
        return {"read":True}

    return router
