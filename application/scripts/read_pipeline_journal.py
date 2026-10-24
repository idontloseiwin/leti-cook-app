"""Read a bounded private pipeline journal; no DB, provider or runtime actions."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from telemetry_contract import MAX_EVENT_MAX_BYTES, normalize_stored_event

MAX_SCAN_BYTES = 512 * 1024 * 1024
MAX_SCAN_RECORDS = 50000


def _unfinished_observations(events):
    """Pair observed occurrences, not mutable reasons or all-time status sets."""
    identity_fields = ("writer_id", "trace_id", "span_id", "operation",
                       "batch_id", "operation_id", "channel_id", "video_id",
                       "job_id", "request_attempt_id")
    pending, ambiguous = defaultdict(list), set()
    for position, event in enumerate(events):
        if not event.get("span_id") or event["operation"] in {"http_started", "provider_waiting"}:
            continue
        key = tuple(event.get(field) for field in identity_fields)
        if event.get("status") == "started":
            pending[key].append((position, event))
        elif event.get("status") and pending.get(key):
            if len(pending[key]) == 1:
                # Unknown is a recorded terminal observation, never proof of a
                # committed write. The original event/outcome remains unchanged.
                del pending[key]
            else:
                # Overlapping repetitions lack an attempt identity. Guessing
                # FIFO/LIFO could close a different write occurrence.
                ambiguous.add(key)
    result = []
    for key, starts in pending.items():
        for position, event in starts:
            result.append((position, {field: event.get(field) for field in identity_fields} | {
                "reason": event.get("reason"), "start_event_id": event["id"],
                "sequence": event.get("sequence"),
                "matching": "ambiguous_repeated_identity" if key in ambiguous else "terminal_not_observed",
            }))
    return [value for _, value in sorted(result)]


def _transport_summary(events):
    """Per-writer intervals: never combine independent monotonic clock origins."""
    starts, intervals, sizes = {}, defaultdict(list), defaultdict(Counter)
    unmatched = 0
    for event in events:
        key = (event.get("writer_id"), event.get("span_id"))
        operation = event.get("operation")
        if operation == "http_started":
            if key in starts:
                unmatched += 1
            starts[key] = event
            endpoint = event.get("endpoint", "unknown")
            if type(event.get("requested_count")) is int:
                sizes[endpoint][event["requested_count"]] += 1
        elif operation == "http_finished":
            start = starts.pop(key, None)
            if start is None:
                unmatched += 1
                continue
            left, right = start.get("monotonic_ns"), event.get("monotonic_ns")
            if not key[0] or not key[1] or type(left) is not int or type(right) is not int or right < left:
                unmatched += 1
                continue
            intervals[key[0]].append((left, right, start.get("endpoint", "unknown")))
    writers = []
    for writer, rows in intervals.items():
        # Exact for recorded pairs only. Missing starts/ends are explicitly unknown.
        groups = {"all_transport": rows,
                  "video_transport": [r for r in rows if r[2] in {"videos", "videos.list", "videos:batchGetStats"}]}
        for group, values in groups.items():
            if not values:
                continue
            points = sorted([(left, 1) for left, right, _ in values if right > left] +
                            [(right, -1) for left, right, _ in values if right > left])
            active = peak = 0
            for _, delta in points:
                active += delta
                peak = max(peak, active)
            width = max(r[1] for r in values) - min(r[0] for r in values)
            total = sum(r[1] - r[0] for r in values)
            writers.append({"writer_id": writer, "group": group, "paired_requests": len(values),
                            "observed_peak": peak, "window_ms": width / 1_000_000,
                            "average_in_observed_window": total / width if width else None})
    return {"scope": "recorded_pairs_per_writer_not_installation_wide",
            "unpaired_boundaries": unmatched + len(starts), "writers": writers,
            "request_sizes": {key: dict(sorted(value.items())) for key, value in sizes.items()}}


def read_journal(path, *, sync_run_id=None, trace_id=None, video_id=None, channel_id=None,
                 limit=200, max_bytes=32 * 1024 * 1024, max_records=MAX_SCAN_RECORDS):
    if not 1 <= limit <= 5000 or not 1 <= max_bytes <= MAX_SCAN_BYTES or not 1 <= max_records <= MAX_SCAN_RECORDS:
        raise ValueError("journal_read_bounds_invalid")
    records = deque(maxlen=max_records)
    invalid = dropped_prefix = 0
    with Path(path).open("rb") as source:
        before = source.seek(0, 2)
        begin = max(0, before - max_bytes)
        source.seek(begin)
        if begin:
            source.readline(MAX_EVENT_MAX_BYTES + 2)
            # Skip the partial first line; pathological long input stays bounded below.
        while source.tell() < before:
            remaining = before - source.tell()
            line = source.readline(min(MAX_EVENT_MAX_BYTES + 2, remaining))
            if not line:
                break
            if len(line) > MAX_EVENT_MAX_BYTES + 1 or not line.endswith(b"\n"):
                invalid += 1
                while line and not line.endswith(b"\n") and source.tell() < before:
                    line = source.readline(min(MAX_EVENT_MAX_BYTES + 2, before - source.tell()))
                continue
            try:
                value = normalize_stored_event(json.loads(line), max_bytes=MAX_EVENT_MAX_BYTES)
            except (ValueError, UnicodeError, RecursionError):
                value = None
            if value is None:
                invalid += 1
                continue
            if len(records) == max_records:
                dropped_prefix += 1
            records.append(value)
        after = source.seek(0, 2)
    rows = list(records)
    sequences, totals = defaultdict(set), defaultdict(dict)
    for event in rows:
        writer, sequence = event.get("writer_id"), event.get("sequence")
        if writer and type(sequence) is int:
            sequences[writer].add(sequence)
            for key in ("dropped_total", "invalid_total", "sink_errors_total"):
                totals[writer][key] = max(totals[writer].get(key, 0), event.get(key, 0))
    gaps = {writer: sum(max(0, b-a-1) for a,b in zip(sorted(seq), sorted(seq)[1:]))
            for writer, seq in sequences.items()}
    def matches(event):
        return ((sync_run_id is None or event.get("sync_run_id") == sync_run_id)
                and (trace_id is None or event.get("trace_id") == trace_id)
                and (video_id is None or event.get("video_id") == video_id or video_id in event.get("video_ids", []))
                and (channel_id is None or event.get("channel_id") == channel_id))
    matching = [event for event in rows if matches(event)]
    # Correlation is explicit by span ancestry/batch, never by adjacent time or video coincidence.
    selected_operations = {(event.get("trace_id"), event["operation_id"])
                           for event in matching if event.get("operation_id")}
    selected_batches = {event.get("batch_id") for event in matching if event.get("batch_id")}
    related = [event for event in rows if matches(event)
               or (event.get("batch_id") and event["batch_id"] in selected_batches)
               or (event.get("operation_id") and (event.get("trace_id"), event["operation_id"]) in selected_operations)]
    selected_spans = {event.get("span_id") for event in related if event.get("span_id")}
    by_span = defaultdict(list)
    for event in rows:
        if event.get("span_id"):
            by_span[event["span_id"]].append(event)
    pending = list(selected_spans)
    ancestors = set(selected_spans)
    while pending:
        span = pending.pop()
        for event in by_span[span]:
            parent = event.get("parent_span_id")
            if parent and parent not in ancestors:
                ancestors.add(parent)
                pending.append(parent)
    filtered = [event for event in rows if matches(event) or event.get("span_id") in ancestors
                or (event.get("batch_id") and event["batch_id"] in selected_batches)
                or (event.get("operation_id") and (event.get("trace_id"), event["operation_id"]) in selected_operations)]
    open_spans = _unfinished_observations(filtered)
    return {"format": "leti-cook-pipeline-observation-v1", "complete_history": False,
            "coverage": {"file_bytes_at_open": before, "scanned_from_byte": begin,
                         "head_truncated": begin > 0 or dropped_prefix > 0,
                         "records_evicted_from_reader": dropped_prefix,
                         "file_size_changed_during_read": after != before,
                         "invalid_records": invalid, "sequence_gaps": gaps,
                         "writer_sequence_ranges": {writer: {"first": min(seq), "last": max(seq),
                             "prefix_not_observed": min(seq) > 1} for writer, seq in sequences.items()},
                         "writer_cumulative_counters": dict(totals),
                         "matched_with_context": len(filtered), "output_truncated": len(filtered) > limit,
                         "caveat": "Bounded retained observations; crash tails and rotated history may be absent. Shared batch totals are not per-video/channel totals."},
            "unfinished_observations": open_spans[:5000],
            "transport": _transport_summary(filtered),
            "context_event_ids": [event["id"] for event in filtered[-limit:] if not matches(event)],
            "events": filtered[-limit:]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--sync-run-id", type=int)
    parser.add_argument("--trace-id")
    parser.add_argument("--video-id")
    parser.add_argument("--channel-id")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        result = read_journal(args.journal, sync_run_id=args.sync_run_id, trace_id=args.trace_id,
                              video_id=args.video_id, channel_id=args.channel_id,
                              limit=args.limit, max_bytes=args.max_bytes)
    except (OSError, ValueError):
        parser.exit(2, "pipeline journal could not be read within the requested bounds\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
