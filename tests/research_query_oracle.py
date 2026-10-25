"""Small synthetic relational oracle, ONLY for tests of the future SQL executor.

Not an application fallback and not evidence of production SQL correctness.
Inputs already represent independently admitted source states; DB projection and
clock-to-state mapping need separate PostgreSQL tests in stages 1.4–1.7.
"""
from datetime import datetime
from functools import cmp_to_key


def cell(row, field):
    key = field.removeprefix("video.")
    value = row.get(key)
    state = row.get("states", {}).get(field, "unknown" if value is None else "known")
    return (value if state == "known" else None), state


def interpreted(value, field):
    if value is None:
        return None
    if field in {"video.views", "video.duration_seconds"}:
        return int(value)
    if field == "video.published_at":
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def condition(node, row):
    if node is None:
        return True
    if "not" in node:
        answer = condition(node["not"], row)
        return None if answer is None else not answer
    for key, decisive, neutral in (("all", False, True), ("any", True, False)):
        if key in node:
            values = [condition(child, row) for child in node[key]]
            if any(value is decisive for value in values):
                return decisive
            return None if any(value is None for value in values) else neutral
    field, op = node["field"], node["op"]
    raw, state = cell(row, field)
    if op == "state_is":
        return state == node["value"]
    if op in {"is_known", "is_unknown"}:
        return (state == "known") == (op == "is_known")
    if raw is None:
        return None
    if op == "in":
        return interpreted(raw, field) in [interpreted(value, field) for value in node["value"]]
    if op == "contains_casefold_v1":
        return node["value"].casefold() in raw.casefold()
    value, target = interpreted(raw, field), interpreted(node["value"], field)
    return {"eq": lambda: value == target, "gt": lambda: value > target,
            "gte": lambda: value >= target, "lt": lambda: value < target,
            "lte": lambda: value <= target}[op]()


def compare(left, right, order):
    for item in order:
        field = item["field"]
        a, b = interpreted(cell(left, field)[0], field), interpreted(cell(right, field)[0], field)
        if a is None or b is None:
            if a is not b:
                return 1 if a is None else -1  # NULLS LAST in both directions.
        else:
            if field == "video.title":
                a, b = a.casefold(), b.casefold()
            if a != b:
                direction = -1 if item["direction"] == "desc" else 1
                return direction * (1 if a > b else -1)
    return (left["id"] > right["id"]) - (left["id"] < right["id"])


def execute_fixture(fixture, query):
    scope = query["scope"]
    channels = fixture["channels"]
    if scope["kind"] == "folder":
        if scope["id"] not in fixture["folders"]:
            return {"error": "source_unavailable"}
        folder = fixture["folders"].index(scope["id"])
        selected = {i for i, item in enumerate(channels) if item["folder"] == folder and not item["hidden"]}
    else:
        selected = {i for i, item in enumerate(channels) if item["id"] == scope["id"] and not item["hidden"]}
        if not selected:
            return {"error": "source_unavailable"}
    complete = all(channels[i]["inventory"] == "complete" for i in selected)
    unique = {}
    for item in fixture["rows"]:
        if item["channel"] not in selected or item["type"] != query["video_types"][0]:
            continue
        # Collection hold is not a deletion/read prohibition. No collection is run.
        if (item.get("proof", "resolved") != "resolved" or item.get("availability", "public") != "public"
                or item.get("publication") is not None
                or (item["type"] == "stream" and item.get("stream_completion") != "proven")):
            complete = False
            continue
        row = {**item, "channel_id": channels[item["channel"]]["id"]}
        date = cell(row, "video.published_at")[0]
        if date and interpreted(date, "video.published_at") > interpreted(fixture["now"], "video.published_at"):
            complete = False
            continue
        if any(cell(row, key)[1] != "known" for key in query.get("coverage", {}).get("required_fields", [])):
            complete = False
            continue
        unique[row["id"]] = row  # JOIN duplication must never duplicate videos.
    if query.get("coverage", {}).get("mode") == "require_complete" and not complete:
        return {"error": "incomplete_source"}
    rows = [row for row in unique.values() if condition(query.get("where"), row) is True]
    ordered = sorted(rows, key=cmp_to_key(lambda a, b: compare(a, b, query["order_by"])))
    return {"ids": [row["id"] for row in ordered[:query.get("page", {}).get("size", 50)]]}
