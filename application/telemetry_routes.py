"""Bounded in-process telemetry HTTP and SSE routes for the local interface."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from telemetry import read_events_since, telemetry_tail_snapshot


router = APIRouter()
# Concurrent live streams. A browser over the cap is told to reconnect later
# instead of receiving an error, which would stop EventSource for good.
MAX_TELEMETRY_STREAMS = 4
BUSY_RETRY_MILLISECONDS = 5000
_streams = {"active": 0}


def _event_id(event):
    return str(event.get("id") or "") if isinstance(event, dict) else ""


def _format_event(event):
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
    current_id = _event_id(event)
    return (
        f"id: {current_id}\ndata: {payload}\n\n"
        if current_id
        else f"data: {payload}\n\n"
    )


@router.get("/telemetry/recent")
def telemetry_recent(limit: int = 120):
    snapshot = telemetry_tail_snapshot(limit=max(1, min(int(limit or 120), 500)))
    if snapshot.get("read_error"):
        return JSONResponse(
            {"status": "unavailable", "events": []}, status_code=503,
            headers={"X-Telemetry-Status": "unavailable", "Cache-Control": "no-store"},
        )
    return {"status": "ok", "events": snapshot["events"]}


def _format_source_status(unavailable):
    # Transport status is separate from domain events and never advances the cursor.
    status = "unavailable" if unavailable else "ok"
    return f'event: telemetry-status\ndata: {{"status":"{status}"}}\n\n'


@router.get("/telemetry/stream")
async def telemetry_stream(request: Request):
    last_event_id = request.headers.get("last-event-id") or request.query_params.get(
        "lastEventId"
    )

    async def generator():
        # Counted only while the generator runs, so an unstarted response holds no slot.
        if _streams["active"] >= MAX_TELEMETRY_STREAMS:
            yield f"retry: {BUSY_RETRY_MILLISECONDS}\n: busy\n\n"
            return
        _streams["active"] += 1
        try:
            # File lock and journal read run in a worker thread, not on the event loop.
            snapshot = await asyncio.to_thread(telemetry_tail_snapshot, 500, flush=False)
            unavailable = bool(snapshot.get("read_error"))
            position = snapshot["position"]
            generation = snapshot["generation"]
            replay = [] if unavailable else snapshot["events"]
            if last_event_id:
                for index, event in enumerate(replay):
                    if _event_id(event) == last_event_id:
                        replay = replay[index + 1 :]
                        break
            started = time.monotonic()
            yield "retry: 2000\n: legacy-fallback\n\n"
            yield _format_source_status(unavailable)
            for event in replay:
                yield _format_event(event)
            while time.monotonic() - started < 30:
                if await request.is_disconnected():
                    break
                update = await asyncio.to_thread(
                    read_events_since, position, generation, 500, flush=False
                )
                read_error = bool(update.get("read_error"))
                if read_error != unavailable:
                    yield _format_source_status(read_error)
                unavailable = read_error
                if not read_error:
                    position = update["position"]
                    generation = update["generation"]
                    for event in update["events"]:
                        yield _format_event(event)
                await asyncio.sleep(0.5)
        finally:
            _streams["active"] -= 1

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
