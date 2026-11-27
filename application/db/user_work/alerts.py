"""Local rule storage, confirmed snapshot pairs and durable alert delivery."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from psycopg.types.json import Jsonb
from alerts import AlertRule, COUNTERS, Measurement, crossed
from youtube_source_fields import youtube_duration_seconds
from ..base.postgres import DatabaseServerError, Operation
from ..base.sql_values import application_value

BATCH = 100
MAX_RULES = 100
PURGE_BATCH = 1000
_RULE_FIELDS = "id,version,definition,enabled,started_at,gap_count,last_gap_at"
_INBOX_COLUMNS = """n.id::text,n.rule_id,n.version,n.rule_name,n.event,n.direction,n.threshold::text,
 n.subject,n.channel_id,n.video_type,n.observed_at,n.previous_at,n.previous_value::text,
 n.current_value::text,n.published_at,least(n.expires_at,
 CASE WHEN v.title IS NOT NULL THEN v.metadata_checked_at+interval '696 hours' END,h.source_expires_at) AS source_expires_at,
 n.is_read,v.title AS video_title,
 coalesce(v.availability_status='public' AND v.publication_status IS NULL AND h.video_type=n.video_type,false) AS can_open_video"""

# Read only captured observation IDs. The preceding comparable known value is
# found by its source clock, independently of processor order or commit IDs.
_PAIR_COLUMNS = {"video.views": "views", "video.likes": "like_count", "video.comments": "comment_count"}


def video_pair_query(event):
    column = _PAIR_COLUMNS[event]
    views = event == "video.views"
    clock = "s.recorded_at" if views else "coalesce(s.engagement_observed_at,s.recorded_at)"
    epoch = "s.views_definition_epoch" if views else "'reported'"
    return f"""SELECT j.id,s.{column} AS current_value,{epoch} AS current_epoch,
        prev.value AS previous_value,prev.at AS previous_at,prev.epoch AS previous_epoch
      FROM public.alert_observations j
      JOIN public.video_snapshots s ON s.id=CASE WHEN pg_input_is_valid(j.source_key, 'bigint') THEN j.source_key::bigint END AND s.video_id=j.subject
      LEFT JOIN LATERAL (SELECT s.{column} AS value,{clock} AS at,{epoch} AS epoch
        FROM public.video_snapshots s WHERE s.video_id=j.subject AND s.{column} IS NOT NULL
        AND s.recorded_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
        AND {clock}<j.observed_at AND {clock}>=j.available_from
        ORDER BY {clock} DESC,s.id DESC LIMIT 1) prev ON true
      WHERE j.id=ANY(%s::bigint[])"""


_CHANNEL_PAIR = """SELECT j.id,s.subscribers AS current_value,'reported' AS current_epoch,
 p.subscribers AS previous_value,p.recorded_at AS previous_at,'reported' AS previous_epoch
 FROM public.alert_observations j JOIN public.channel_snapshots s ON s.channel_id=j.subject
 AND s.recorded_at=j.observed_at
 LEFT JOIN LATERAL (SELECT subscribers,recorded_at FROM public.channel_snapshots
 WHERE channel_id=j.subject AND subscribers IS NOT NULL AND recorded_at<j.observed_at
 AND recorded_at>=j.available_from AND source='youtube_channel_metadata_refresh'
 ORDER BY recorded_at DESC LIMIT 1) p ON true WHERE j.id=ANY(%s::bigint[])"""

_INSERT = """INSERT INTO public.alert_inbox (
 rule_id,version,rule_name,event,direction,threshold,subject,channel_id,channel_ref,video_type,
 source_key,observed_at,previous_at,previous_value,current_value,published_at,expires_at)
 SELECT x.target,x.version,x.name,j.event,x.direction,x.threshold,j.subject,j.channel_id,j.channel_ref,j.video_type,
 CASE WHEN j.event='video.published' THEN j.subject ELSE j.source_key END,
 j.observed_at,x.previous_at,x.previous_value,x.current_value,
 CASE WHEN j.event='video.published' THEN j.published_at END,
 least(j.evidence_expires_at,x.previous_at+interval '696 hours')
 FROM jsonb_to_recordset(%s::jsonb) x(id bigint,target uuid,version integer,name text,direction text,threshold bigint,
 previous_at timestamptz,previous_value bigint,current_value bigint)
 JOIN public.alert_observations j USING(id) JOIN public.alert_rules r ON r.id=x.target AND r.deleted_at IS NULL
 ORDER BY j.observed_at,j.id,x.target
 ON CONFLICT DO NOTHING"""

RETRY_SECONDS = 15
_NORMAL_IDS = """SELECT id FROM public.alert_observations WHERE retry_at IS NULL
    ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"""
_RETRY_ID = """SELECT id FROM public.alert_observations WHERE retry_at<=statement_timestamp()
    ORDER BY retry_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"""
def _live_queue_ids(retry):
    # A deleted rule may own the first million IDs. Seek by each visible rule
    # only while cleanup is in progress. Paused rules retain accepted work.
    due = "retry_at <= statement_timestamp()" if retry else "retry_at IS NULL"
    order = "retry_at,id" if retry else "id"
    limit = 1 if retry else BATCH
    ordered = ','.join('q.' + column for column in order.split(','))
    return f"""WITH candidates AS MATERIALIZED (
        SELECT q.* FROM public.alert_rules r CROSS JOIN LATERAL (
            SELECT id,retry_at FROM public.alert_observations
            WHERE rule_id=r.id AND {due} ORDER BY {order} LIMIT {limit}
        ) q WHERE r.deleted_at IS NULL
        UNION ALL (SELECT id,retry_at FROM public.alert_observations WHERE members IS NOT NULL
            AND {due} ORDER BY {order} LIMIT {limit})), picked AS MATERIALIZED (
        SELECT o.id FROM candidates q JOIN public.alert_observations o USING(id)
        WHERE o.{due} ORDER BY {ordered} LIMIT {limit} FOR UPDATE OF o SKIP LOCKED)
        , fallback AS (
            SELECT q.id FROM public.alert_observations q LEFT JOIN public.alert_rules r ON r.id=q.rule_id
            WHERE NOT EXISTS (SELECT FROM picked) AND EXISTS (SELECT FROM candidates)
            AND (q.members IS NOT NULL OR r.id IS NOT NULL AND r.deleted_at IS NULL) AND q.{due}
            ORDER BY {ordered} LIMIT {limit} FOR UPDATE OF q SKIP LOCKED)
        SELECT id FROM picked UNION ALL SELECT id FROM fallback"""


_LIVE_NORMAL_IDS = _live_queue_ids(False)
_LIVE_RETRY_ID = _live_queue_ids(True)

# Reject dates outside the driver's supported range in SQL, before row decoding.
_OBSERVATIONS = """SELECT id,rule_id::text AS target,version,definition::text AS definition_json,members IS NOT NULL AS shared,
    event,video_type,duration,published_at,observed_at,
    CASE WHEN members IS NULL THEN available_from END AS available_from,
    CASE WHEN members IS NULL THEN rule_started_at END AS rule_started_at,
    evidence_expires_at,statement_timestamp() AS evaluated_at
    FROM public.alert_observations WHERE id=ANY(%s::bigint[])
    AND least(observed_at,evidence_expires_at,published_at,
            CASE WHEN members IS NULL THEN available_from END,CASE WHEN members IS NULL THEN rule_started_at END)
        >= timestamptz '0001-01-01 00:00 UTC'
    AND greatest(observed_at,evidence_expires_at,published_at,
            CASE WHEN members IS NULL THEN available_from END,CASE WHEN members IS NULL THEN rule_started_at END)
        < timestamptz '10000-01-01 00:00 UTC'"""


# Decode each frozen definition independently so invalid data in one rule
# cannot prevent processing its neighbors.
_MEMBERS = """SELECT j.id,r.id::text AS target,m.value->>'version' AS version,
    (m.value->'rule')::text AS definition_json,m.value->>'since' AS since,m.value->>'started' AS started
    FROM public.alert_observations j CROSS JOIN LATERAL jsonb_each(j.members) m
    JOIN public.alert_rules r ON r.id::text=m.key AND r.deleted_at IS NULL
    WHERE j.id=ANY(%s::bigint[])"""

_SPLIT_ID = """SELECT id FROM public.alert_observations WHERE id=%s AND members IS NOT NULL
    AND (retry_at IS NULL OR retry_at<=statement_timestamp())
    AND EXISTS (SELECT FROM jsonb_object_keys(members) OFFSET 1) FOR UPDATE SKIP LOCKED"""
_SPLIT = """INSERT INTO public.alert_observations(event,subject,channel_id,channel_ref,source_key,observed_at,
        available_from,rule_started_at,evidence_expires_at,video_type,published_at,duration,
        enqueued_at,retry_at,members)
    SELECT j.event,j.subject,j.channel_id,j.channel_ref,j.source_key,j.observed_at,
        j.available_from,j.rule_started_at,j.evidence_expires_at,j.video_type,j.published_at,j.duration,
        j.enqueued_at,statement_timestamp(),jsonb_build_object(m.key,m.value)
    FROM public.alert_observations j CROSS JOIN LATERAL jsonb_each(j.members) m WHERE j.id=%s"""

class Conflict(ValueError):
    pass


class AlertsRepository:
    def __init__(self, database):
        self.database = database
        self._prefer_retry = True
        self._last_cleanup_id = None

    def rules(self):
        return self.database.read_rows("alert_rules", f"SELECT {_RULE_FIELDS},source_available,retry_pending FROM readable.alert_rules ORDER BY started_at DESC,id LIMIT 100",
                                       max_rows=MAX_RULES)

    @staticmethod
    def _scope_owner(tx, scope):
        if scope.kind == "channel":
            sql = """SELECT c.user_work_id AS owner FROM public.channels c
              JOIN public.channel_collection_state control USING(channel_id,user_work_id)
              WHERE c.channel_id=%s AND NOT control.is_hidden"""
        elif scope.kind == "video":
            sql = """SELECT c.user_work_id AS owner FROM readable.hot_video_inventory h
              JOIN public.channels c USING(channel_id) JOIN public.channel_collection_state control USING(channel_id,user_work_id)
              WHERE h.video_id=%s AND NOT control.is_hidden"""
        elif scope.kind == "folder":
            sql = "SELECT alert_identity AS owner,created_at FROM public.folders WHERE id=%s::uuid"
        else:
            sql = "SELECT alert_identity AS owner,created_at FROM public.channel_labels WHERE label_key=%s"
        row = tx.fetch_one(sql, (scope.id,))
        if row is None:
            raise Conflict("alert_source_unavailable")
        return row["owner"], row.get("created_at")

    def create(self, rule_id, rule):
        with self.database.transaction(Operation("alert_create")) as tx:
            tx.fetch_one("SELECT pg_advisory_xact_lock(864297301)")
            existing = tx.fetch_one(f"SELECT {_RULE_FIELDS},deleted_at FROM public.alert_rules WHERE id=%s", (rule_id,))
            definition = rule.model_dump(mode="json", exclude_none=True)
            if existing:
                if existing.pop("deleted_at") is None and existing["definition"] == definition and existing["version"] == 1:
                    return application_value(existing)
                raise Conflict("alert_id_already_used")
            if tx.fetch_one("SELECT count(*) AS count FROM public.alert_rules WHERE deleted_at IS NULL")["count"] >= MAX_RULES:
                raise Conflict("alert_rule_limit")
            owner, created_at = self._scope_owner(tx, rule.scope)
            return application_value(tx.returning_one(f"""INSERT INTO public.alert_rules(id,definition,scope_owner,scope_created_at)
                VALUES(%s,%s,%s,%s) RETURNING {_RULE_FIELDS}""", (rule_id, Jsonb(definition), owner, created_at)))

    def change(self, rule_id, version, rule, enabled):
        with self.database.transaction(Operation("alert_change")) as tx:
            row = tx.fetch_one(f"SELECT {_RULE_FIELDS},scope_owner,scope_created_at FROM public.alert_rules WHERE id=%s AND deleted_at IS NULL FOR NO KEY UPDATE", (rule_id,))
            if row is None or row["version"] != version:
                raise Conflict("alert_rule_changed")
            definition = rule.model_dump(mode="json", exclude_none=True) if rule else row["definition"]
            active = enabled if enabled is not None else row["enabled"]
            if row["definition"] == definition and active == row["enabled"]:
                return application_value({k: row[k] for k in _RULE_FIELDS.split(",")})
            owner, created_at = row["scope_owner"], row["scope_created_at"]
            new_scope = definition["scope"] != row["definition"]["scope"]
            if active or new_scope:
                identity = self._scope_owner(tx, AlertRule.model_validate(definition).scope)
                if not new_scope and identity != (owner, created_at):
                    raise Conflict("alert_source_replaced")
                owner, created_at = identity
            semantic_change = active != row["enabled"] or any(
                definition.get(k) != row["definition"].get(k)
                for k in (definition.keys() | row["definition"].keys()) - {"name"})
            return application_value(tx.returning_one(f"""UPDATE public.alert_rules SET definition=%s,scope_owner=%s,
                scope_created_at=%s,enabled=%s,version=version+1,
                started_at=CASE WHEN %s THEN clock_timestamp() ELSE started_at END
                WHERE id=%s RETURNING {_RULE_FIELDS}""",
                (Jsonb(definition), owner, created_at, active, semantic_change, rule_id)))

    def delete(self, rule_id, version):
        if not self.database.write("alert_delete", """UPDATE public.alert_rules SET enabled=false,deleted_at=clock_timestamp()
            WHERE id=%s AND version=%s AND deleted_at IS NULL""", (rule_id,version)):
            raise Conflict("alert_rule_changed")

    def inbox(self, before, formats):
        return self.database.read_rows("alert_inbox", f"""WITH page AS MATERIALIZED (
            SELECT * FROM readable.alert_inbox n WHERE n.id<%s AND
            (n.video_type IS NULL OR n.video_type=ANY(%s::text[])) ORDER BY n.id DESC LIMIT 51)
            SELECT {_INBOX_COLUMNS} FROM page n
            LEFT JOIN readable.videos v ON n.video_type IS NOT NULL AND v.video_id=n.subject AND v.channel_id=n.channel_id
            LEFT JOIN LATERAL (SELECT i.video_type,i.source_expires_at FROM readable.hot_video_inventory i WHERE v.video_id=i.video_id
                AND i.channel_id=n.channel_id LIMIT 1) h ON true ORDER BY n.id DESC""",
            (before, list(formats)), max_rows=51)

    def read(self, item_id, formats):
        count = self.database.write("alert_mark_read", """UPDATE public.alert_inbox n SET is_read=true
          FROM readable.channel_user_work c WHERE n.id=%s AND n.expires_at>statement_timestamp()
          AND EXISTS (SELECT FROM public.alert_rules r WHERE r.id=n.rule_id AND r.deleted_at IS NULL)
          AND c.channel_id=n.channel_id AND c.user_work_id=n.channel_ref AND NOT c.is_hidden
          AND (n.video_type IS NULL OR n.video_type=ANY(%s::text[]))""", (item_id,list(formats)))
        return bool(count)

    def _purge_deleted(self, tx):
        # Rotate even when a reader prevents final deletion. A busy first rule
        # must not prevent cleanup of another rule on the following pass.
        rule = tx.fetch_one("""SELECT id FROM public.alert_rules WHERE deleted_at IS NOT NULL
            ORDER BY (id<=%s::uuid),id LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED""", (self._last_cleanup_id,))
        if rule is None:
            return 0
        self._last_cleanup_id = rule["id"]
        args = (rule["id"], PURGE_BATCH)
        removed = 0
        for table in ("public.alert_observations", "public.alert_inbox"):
            removed += tx.execute(f"""DELETE FROM {table} WHERE id IN (
                SELECT id FROM {table} WHERE rule_id=%s LIMIT %s FOR UPDATE SKIP LOCKED)""", args)
        # Unlink only this owner. A shared row may be locked by delivery.
        page = tx.fetch_all("""SELECT id,(members-%s::text)='{}'::jsonb AS last_member
            FROM public.alert_observations WHERE members IS NOT NULL AND members ? %s::text
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
            (str(rule["id"]), str(rule["id"]), PURGE_BATCH), max_rows=PURGE_BATCH)
        if page:
            terminal = [row["id"] for row in page if row["last_member"]]
            shared = [row["id"] for row in page if not row["last_member"]]
            if terminal:
                removed += tx.execute("DELETE FROM public.alert_observations WHERE id=ANY(%s::bigint[])", (terminal,))
            if shared:
                removed += tx.execute("UPDATE public.alert_observations SET members=members-%s::text WHERE id=ANY(%s::bigint[])",
                                      (str(rule["id"]), shared))
        if removed:
            return removed
        # A capture with an older snapshot can still be inserting. Acquire the
        # exclusive parent lock without waiting, then recheck with a new snapshot.
        locked = tx.fetch_one("SELECT id FROM public.alert_rules WHERE id=%s FOR UPDATE SKIP LOCKED", (rule["id"],))
        if locked:
            return tx.execute("""DELETE FROM public.alert_rules WHERE id=%s
                AND NOT EXISTS (SELECT FROM public.alert_observations WHERE rule_id=%s)
                AND NOT EXISTS (SELECT FROM public.alert_observations WHERE members IS NOT NULL AND members ? %s::text)
                AND NOT EXISTS (SELECT FROM public.alert_inbox WHERE rule_id=%s)""", (rule["id"],)*4)
        return 0

    def process(self, formats):
        """Return positive progress after delivery, deferral or cleanup commits."""
        ids = []
        cleaned = 0
        # Commit any exclusive rule lock before evaluating unrelated observations.
        with self.database.transaction(Operation("alert_cleanup", statement_timeout_ms=5000, disable_jit=True)) as tx:
            deleting = tx.fetch_one("SELECT 1 AS pending FROM public.alert_rules WHERE deleted_at IS NOT NULL LIMIT 1")
            if deleting:
                cleaned = self._purge_deleted(tx)
        try:
            with self.database.transaction(Operation("alert_observe", statement_timeout_ms=5000,
                                                     total_timeout_ms=12000, disable_jit=True)) as tx:
                expired = tx.execute("""DELETE FROM public.alert_inbox WHERE id IN (SELECT id FROM public.alert_inbox
                    WHERE expires_at<=statement_timestamp() ORDER BY expires_at LIMIT 500)""")
                if deleting:
                    lanes = ((_LIVE_NORMAL_IDS, (), BATCH), (_LIVE_RETRY_ID, (), 1))
                else:
                    lanes = ((_NORMAL_IDS, (BATCH,), BATCH), (_RETRY_ID, (), 1))
                for sql, params, limit in (reversed(lanes) if self._prefer_retry else lanes):
                    rows = tx.fetch_all(sql, params, max_rows=limit)
                    if rows:
                        ids = [row["id"] for row in rows]
                        self._prefer_retry = sql in (_NORMAL_IDS, _LIVE_NORMAL_IDS)
                        self._deliver(tx, ids, formats)
                        break
        except DatabaseServerError as error:
            if error.code != "57014" or not ids or error.outcome_unknown:
                raise
            # The previous transaction rolled back. Independent workers may already
            # have finished some IDs. Update only work that is still due.
            delay = RETRY_SECONDS if len(ids) == 1 else 0
            with self.database.transaction(Operation("alert_defer", statement_timeout_ms=5000,
                                                     total_timeout_ms=12000)) as tx:
                if len(ids) == 1 and tx.fetch_one(_SPLIT_ID, (ids[0],)):
                    tx.execute(_SPLIT, (ids[0],))
                    tx.execute("DELETE FROM public.alert_observations WHERE id=%s", (ids[0],))
                    return 1
                deferred = tx.execute("""UPDATE public.alert_observations
                    SET retry_at=clock_timestamp()+%s*interval '1 second'
                    WHERE id=ANY(%s::bigint[]) AND (retry_at IS NULL OR retry_at<=statement_timestamp())""",
                           (delay, ids))
            return deferred or cleaned
        return len(ids) or cleaned or expired

    @staticmethod
    def _deliver(tx, ids, formats):
        batch = tx.fetch_all(_OBSERVATIONS, (ids,), max_rows=len(ids))
        missing_ids = set(ids).difference(job["id"] for job in batch)
        targets = []
        by_id = {job["id"]: job for job in batch}
        for job in batch:
            if not job["shared"]:
                targets.append(job)
        shared_ids = [job["id"] for job in batch if job["shared"]]
        if shared_ids:
            targets.extend(tx.fetch_all(_MEMBERS, (shared_ids,), max_rows=len(shared_ids)*MAX_RULES))

        # The same frozen rule occurs on many videos in a batch. Keep this cache
        # local to one transaction so old definitions leave memory promptly.
        parse = lru_cache(maxsize=MAX_RULES*2)(AlertRule.model_validate_json)
        lost = []
        eligible = []
        groups = {}
        for target in targets:
            job = by_id[target["id"]]
            try:
                if job["evidence_expires_at"] <= job["evaluated_at"]:
                    raise ValueError("expired")
                rule = parse(target["definition_json"])
                if rule.event != job["event"] or rule.video_type != job["video_type"]:
                    raise ValueError("inconsistent_definition")
                if job["shared"]:
                    since = datetime.fromisoformat(target["since"])
                    started = datetime.fromisoformat(target["started"])
                    version = int(target["version"])
                    if since.tzinfo is None or started.tzinfo is None or not 1 <= version <= 2147483647:
                        raise ValueError("invalid_recipient")
                else:
                    since, started, version = job["available_from"], job["rule_started_at"], job["version"]
                if job["observed_at"] < since or (rule.video_type is not None and rule.video_type not in formats):
                    continue
                if rule.min_duration is not None or rule.max_duration is not None:
                    duration = youtube_duration_seconds(job["duration"])
                    if duration is None or (rule.min_duration is not None and duration<rule.min_duration) or (
                            rule.max_duration is not None and duration>rule.max_duration):
                        continue
                if rule.event in COUNTERS:
                    groups.setdefault(rule.event,set()).add(job["id"])
                eligible.append((job,target["target"],rule,version,since,started))
            except (TypeError, ValueError, OverflowError):
                lost.append(target["target"])

        pairs = {}
        for event, group in groups.items():
            statement = _CHANNEL_PAIR if event == "channel.subscribers" else video_pair_query(event)
            for p in tx.fetch_all(statement, (list(group),), max_rows=len(group)):
                if p["previous_value"] is None or p["current_value"] is None:
                    pairs[p["id"]] = None
                    continue
                try:
                    pairs[p["id"]] = (
                        Measurement(p["previous_value"], p["previous_at"], p["previous_epoch"]),
                        Measurement(p["current_value"], by_id[p["id"]]["observed_at"], p["current_epoch"]))
                except ValueError:
                    continue

        messages = []
        for job,target,rule,version,since,started in eligible:
            evidence = dict(id=job["id"],target=target,version=version,name=rule.name,
                            direction=rule.direction,threshold=rule.threshold)
            if rule.event in COUNTERS:
                if job["id"] not in pairs:
                    lost.append(target)
                    continue
                pair = pairs[job["id"]]
                if pair is None:
                    continue
                before, after = pair
                if before.at < since or not crossed(rule,before,after,started_at=started,as_of=job["evaluated_at"]):
                    continue
                evidence.update(previous_value=before.value,current_value=after.value,previous_at=before.at.isoformat())
            elif rule.event == "video.published":
                if not job["published_at"] or not max(started,since) < job["published_at"] <= job["observed_at"]:
                    continue
            messages.append(evidence)
        if missing_ids or lost:
            tx.execute("""UPDATE public.alert_rules r
                SET gap_count=r.gap_count+lost.count,last_gap_at=statement_timestamp()
                FROM (SELECT target,count(*) FROM (
                SELECT rule_id::text AS target FROM public.alert_observations WHERE id=ANY(%s::bigint[]) AND members IS NULL
                UNION ALL SELECT jsonb_object_keys(members) FROM public.alert_observations WHERE id=ANY(%s::bigint[])
                UNION ALL SELECT unnest(%s::text[])) affected GROUP BY target) lost
                WHERE r.id::text=lost.target AND r.deleted_at IS NULL""",(list(missing_ids),list(missing_ids),lost))
        if messages:
            tx.execute(_INSERT, (Jsonb(messages),))
        tx.execute("DELETE FROM public.alert_observations WHERE id=ANY(%s::bigint[])", (ids,))
