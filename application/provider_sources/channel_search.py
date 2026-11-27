"""Owner-initiated discovery of channels using public YouTube results."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import secrets
import threading

from .continuation import LIFETIME
from .plan import IDENTITIES
from .service import ServiceRefused
from .wire import acquisition_request, load_document

SEARCH = "search_queries"
GENERAL = "general"
STATS = "video_batch_get_stats"
MAX_ACTIONS = 32
SEARCH_FIELDS = ["snippet.title", "snippet.channelId", "snippet.channelTitle", "snippet.description",
                 "snippet.publishedAt", "snippet.thumbnails.medium.url"]
PROFILE_FIELDS = ["snippet.title", "snippet.publishedAt", "statistics.subscriberCount",
                  "statistics.hiddenSubscriberCount", "statistics.videoCount"]


def _request(method, parameters, parts, fields, bucket):
    return acquisition_request({"version": 1, "method": "youtube." + method, "parameters": parameters,
                                "selection": {"parts": parts, "fields": fields},
                                "budget": {"max_requests": 1, "max_units": {bucket: 1}}})


def _search_request(spec):
    if not isinstance(spec, dict) or set(spec) - {"mode", "q", "category", "after", "stats"}:
        raise ServiceRefused("search_input_invalid", 422)
    mode = spec.get("mode")
    if mode not in ("topic", "channel", "category") or type(spec.get("stats")) is not bool:
        raise ServiceRefused("search_input_invalid", 422)
    params = {"type": "channel" if mode == "channel" else "video", "maxResults": 50}
    if mode == "category":
        category = spec.get("category")
        if not isinstance(category, str) or not category.isascii() or not category.isdecimal() or len(category) > 4 or "q" in spec:
            raise ServiceRefused("category_invalid", 422)
        params["videoCategoryId"] = category
    else:
        query = spec.get("q")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or "category" in spec:
            raise ServiceRefused("search_term_invalid", 422)
        params["q"] = query.strip()
    if mode == "channel" and (spec["stats"] or "after" in spec):
        raise ServiceRefused("video_filter_requires_video_search", 422)
    if "after" in spec:
        params["publishedAfter"] = spec["after"]
    return _request("search.list", params, ["snippet"], SEARCH_FIELDS, SEARCH)


def _values(row):
    return {f: v.get("value") for f, v in row["fields"].items() if v["state"] in ("known", "known_empty")}


def _summary(result):
    return {"cost": result["cost"], "stop": result["stop_reason"],
            "rejected": sum(p["rejected_items"] for p in result["pages"]), "sent": result["requests_sent"]}


@dataclass
class Action:
    owner: str
    expires: float
    spec: dict
    mode: str
    budget: dict
    parent: str | None = None
    state: str = "ready"
    result: dict | None = None


class ChannelSearch:
    """Only issued, unexpired owner actions can spend. Unknown outcomes require a fresh preview."""
    def __init__(self, provider, library_states):
        self.provider = provider
        self.library_states = library_states
        self.actions = {}
        self.lock = threading.Lock()

    def _expire(self, key):
        with self.lock:
            self.actions.pop(key, None)

    def _get(self, key, owner):
        act = self.actions.get(key) if isinstance(key, str) else None
        if not act or act.owner != owner or act.expires <= self.provider.monotonic():
            raise ServiceRefused("search_action_expired", 409)
        return act

    def preview(self, raw, owner):
        body = load_document(raw)
        if not isinstance(body, dict):
            raise ServiceRefused("search_input_invalid", 422)
        now = self.provider.monotonic()
        with self.lock:
            self.actions = {k: v for k, v in self.actions.items() if v.expires > now or v.state == "running"}
            parent = None
            mode = "first"
            if set(body) == {"parent", "action"}:
                parent = body["parent"]
                old = self._get(parent, owner)
                mode = body["action"]
                if old.state != "done" or mode not in ("more", "retry"):
                    raise ServiceRefused("search_action_invalid", 409)
                page = old.result
                if (mode == "more" and not page["continuation"]) or (mode == "retry" and not page["pending"]):
                    raise ServiceRefused("search_action_unavailable", 409)
                spec = old.spec
                # Enriching a page preserves its cursor. All revisions share one next-page action.
                if mode == "more":
                    while old.mode == "retry":
                        parent = old.parent
                        old = self._get(parent, owner)
                for k, v in self.actions.items():
                    if v.owner == owner and v.parent == parent and v.mode == mode:
                        return {"action_id": k, "max_cost": v.budget}
            else:
                spec = body
            self.provider._plan(_search_request(spec))
            budget = {SEARCH: 1, GENERAL: 1}
            if spec["stats"]:
                budget[STATS] = 1
            if mode == "retry":
                budget = {k: 1 for k in page["pending"]}
            if len(self.actions) >= MAX_ACTIONS:
                raise ServiceRefused("search_session_full", 429)
            key = secrets.token_urlsafe(24)
            expiry = old.expires if parent else now + LIFETIME.total_seconds()
            self.actions[key] = Action(owner, expiry, spec, mode, budget, parent)
            timer = threading.Timer(max(0, expiry - now), self._expire, args=(key,))
            timer.daemon = True
            timer.start()
        return {"action_id": key, "max_cost": budget}

    def execute(self, raw, owner, cancelled, recheck):
        body = load_document(raw)
        if not isinstance(body, dict) or set(body) != {"action_id", "confirmed_cost"}:
            raise ServiceRefused("search_input_invalid", 422)
        with self.lock:
            act = self._get(body["action_id"], owner)
            confirmed = body["confirmed_cost"]
            if not isinstance(confirmed, dict) or any(type(v) is not int for v in confirmed.values()) or confirmed != act.budget:
                raise ServiceRefused("cost_not_confirmed", 409)
            if act.state == "done":
                return self._public(act)
            if act.state != "ready":
                raise ServiceRefused("search_action_" + act.state, 409)
            previous = self._get(act.parent, owner).result if act.parent else None
            act.state = "running"
        entered = False
        try:
            with self.provider.admit(act.budget) as session:
                entered = True
                deadline = self.provider.monotonic() + 60
                def run(request, continuation=None):
                    if recheck() != owner:
                        raise ServiceRefused("policy_denied", 403)
                    return self.provider.run_plan(request, self.provider._plan(request), cancelled, session,
                                                  deadline, continuation)
                result = self._page(act, previous, run)
        except BaseException:
            with self.lock:
                act.state = "uncertain" if entered else "ready"
            raise
        with self.lock:
            self._get(body["action_id"], owner)
            act.state, act.result = "done", result
        return self._public(act)

    def _public(self, act):
        result = act.result
        return {"valid_for_ms": max(0, int((act.expires - self.provider.monotonic()) * 1000)),
                **{k: v for k, v in result.items() if k not in ("continuation", "pending")},
                "more": bool(result["continuation"]), "retry": bool(result["pending"]),
                "retry_cost": {k: 1 for k in result["pending"]}}

    def _page(self, act, previous, run):
        if act.mode == "retry":
            page = deepcopy(previous)
            page["steps"] = {}
        else:
            cursor = previous["continuation"] if act.mode == "more" else None
            result = run(_search_request(act.spec), cursor)
            page = {"hits": [], "profiles": {}, "views": {}, "pending": {}, "library": {},
                    "continuation": result["continuation"], "steps": {SEARCH: _summary(result)}}
            for row in result["rows"]:
                ident, fields = row["identity"], _values(row)
                if not ident:
                    continue
                expected = "channel" if act.spec["mode"] == "channel" else "video"
                if ident["kind"] != expected:
                    page["steps"][SEARCH]["rejected"] += 1
                    continue
                channel = ident["id"] if expected == "channel" else fields.get("snippet.channelId")
                if not isinstance(channel, str) or not IDENTITIES["channel"].fullmatch(channel):
                    channel = None
                page["hits"].append({"kind": ident["kind"], "id": ident["id"], "channel": channel,
                                     "position": row.get("source_position"),
                                     "title": fields.get("snippet.title"), "author": fields.get("snippet.channelTitle"),
                                     "description": fields.get("snippet.description"),
                                     "published": fields.get("snippet.publishedAt"), "thumbnail": fields.get("snippet.thumbnails.medium.url")})
            page["search_step"] = dict(page["steps"][SEARCH])
            authors = list(dict.fromkeys(h["channel"] for h in page["hits"] if h["channel"]))
            if authors:
                page["pending"][GENERAL] = authors
            videos = list(dict.fromkeys(h["id"] for h in page["hits"] if h["kind"] == "video"))
            if act.spec["stats"] and videos:
                page["pending"][STATS] = videos
        for bucket, ids in list(page["pending"].items()):
            if bucket == GENERAL:
                request = _request("channels.list", {"id": ids}, ["snippet", "statistics"], PROFILE_FIELDS, bucket)
            else:
                request = _request("videos.batchGetStats", {"id": ids}, ["id", "statistics"], ["statistics.viewCount"], bucket)
            result = run(request)
            page["steps"][bucket] = _summary(result)
            retry_ids = set(ids)
            for row in result["rows"]:
                if not row["identity"]:
                    continue
                ident = row["identity"]["id"]
                if ident not in retry_ids:
                    page["steps"][bucket]["rejected"] += 1
                    continue
                fields = _values(row)
                if bucket == GENERAL:
                    page["profiles"][ident] = {"name": fields.get("snippet.title"), "created": fields.get("snippet.publishedAt"),
                                               "subscribers": fields.get("statistics.subscriberCount"),
                                               "videos": fields.get("statistics.videoCount")}
                else:
                    page["views"][ident] = fields.get("statistics.viewCount")
                retry_ids.discard(ident)
            if retry_ids:
                page["pending"][bucket] = sorted(retry_ids)
            else:
                del page["pending"][bucket]
        authors = list(dict.fromkeys(h["channel"] for h in page["hits"] if h["channel"]))
        if authors and self.library_states is not None:
            page["library"] = self.library_states(authors)
        return page
