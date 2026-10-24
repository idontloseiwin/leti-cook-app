"""Bounded, non-blocking telemetry emission and JSONL journal boundary."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any, Iterable

from telemetry_contract import (
    TelemetryContractError,
    build_event,
    encode_event,
    event_max_bytes,
    normalize_stored_event,
)

try:
    import fcntl
except Exception:  # pragma: no cover - Windows/local development fallback.
    fcntl = None


DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_RECENT_LIMIT = 300
DEFAULT_QUEUE_SIZE = 4096
DEFAULT_BATCH_SIZE = 32
DEFAULT_LOCK_TIMEOUT_SECONDS = 1.0
MAX_READ_BYTES = 1024 * 1024

_PROCESS_FILE_LOCK = threading.Lock()
_EMITTER_LOCK = threading.Lock()
_EMITTERS: dict[tuple[str, int, int, int], "TelemetryEmitter"] = {}


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    if value != value or value in (float("inf"), float("-inf")):
        value = default
    return max(minimum, min(value, maximum))


def configured_path() -> Path:
    configured = os.getenv("TELEMETRY_PATH")
    if configured:
        return Path(configured)
    # The installation already provisions this private, writable state volume.
    # A trace in /tmp would disappear on every backend recreation.
    auth_database = os.getenv("LIC_AUTH_DATABASE", "")
    if os.getenv("LIC_DEPLOYMENT_PROFILE") == "self-hosted" and auth_database:
        auth_path = Path(auth_database)
        if auth_path.is_absolute():
            return auth_path.parent / "pipeline-telemetry.jsonl"
    return Path(tempfile.gettempdir()) / "youtube-tracker-telemetry.jsonl"


def configured_max_bytes() -> int:
    return _bounded_int(
        "TELEMETRY_MAX_BYTES", DEFAULT_MAX_BYTES, 1024, 512 * 1024 * 1024
    )


def configured_recent_limit() -> int:
    return _bounded_int("TELEMETRY_RECENT_LIMIT", DEFAULT_RECENT_LIMIT, 1, 2000)


def configured_event_max_bytes() -> int:
    return event_max_bytes(os.getenv("TELEMETRY_EVENT_MAX_BYTES"))


def configured_queue_size() -> int:
    return _bounded_int("TELEMETRY_QUEUE_SIZE", DEFAULT_QUEUE_SIZE, 16, 8192)


def configured_batch_size() -> int:
    return _bounded_int("TELEMETRY_BATCH_SIZE", DEFAULT_BATCH_SIZE, 1, 256)


def configured_lock_timeout() -> float:
    return _bounded_float(
        "TELEMETRY_LOCK_TIMEOUT_SECONDS",
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        0.01,
        10.0,
    )


def _generation_from_stat(stat_result: os.stat_result) -> str:
    inode = getattr(stat_result, "st_ino", 0) or 0
    device = getattr(stat_result, "st_dev", 0) or 0
    if inode or device:
        return f"{device}:{inode}"
    ctime_ns = getattr(
        stat_result,
        "st_ctime_ns",
        int(stat_result.st_ctime * 1_000_000_000),
    )
    return f"ctime:{ctime_ns}"


@contextlib.contextmanager
def journal_file_lock(path: Path, *, timeout: float | None = None):
    """Take the process and advisory journal lock with a bounded wait."""

    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = configured_lock_timeout() if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + timeout
    acquired_process = _PROCESS_FILE_LOCK.acquire(timeout=timeout)
    if not acquired_process:
        raise TimeoutError("telemetry process lock timed out")
    handle = None
    try:
        lock_path = path.with_name(f"{path.name}.lock")
        handle = lock_path.open("a+b")
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("telemetry file lock timed out")
                    time.sleep(0.01)
        yield
    finally:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        _PROCESS_FILE_LOCK.release()


class JsonlTelemetryJournal:
    """Bounded persistent journal; all filesystem I/O lives behind this type."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_bytes: int | None = None,
        event_bytes: int | None = None,
        lock_timeout: float | None = None,
    ):
        self.path = Path(path) if path is not None else configured_path()
        self.max_bytes = max(1024, int(max_bytes or configured_max_bytes()))
        self.event_bytes = event_max_bytes(event_bytes or configured_event_max_bytes())
        self.lock_timeout = configured_lock_timeout() if lock_timeout is None else lock_timeout

    def _state_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"position": 0, "generation": None, "size": 0}
        stat_result = self.path.stat()
        return {
            "position": stat_result.st_size,
            "generation": _generation_from_stat(stat_result),
            "size": stat_result.st_size,
        }

    def state(self) -> dict[str, Any]:
        try:
            with journal_file_lock(self.path, timeout=self.lock_timeout):
                return self._state_locked()
        except Exception:
            return {"position": 0, "generation": None, "size": 0, "read_error": True}

    def _rotate_locked(self) -> None:
        if not self.path.exists() or self.path.stat().st_size <= self.max_bytes:
            return
        keep_bytes = max(self.max_bytes // 2, 1024)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                delete=False,
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            ) as handle:
                temporary = Path(handle.name)
                with self.path.open("rb") as source:
                    source.seek(max(0, self.path.stat().st_size - keep_bytes))
                    # Discard only a possible leading fragment; the tail is
                    # copied in 64 KiB chunks even for a 512 MiB journal.
                    source.readline(self.event_bytes + 2)
                    while chunk := source.read(65536):
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                with contextlib.suppress(Exception):
                    temporary.unlink()

    def append_batch(self, events: Iterable[dict[str, Any]]) -> int:
        lines = [encode_event(event) + b"\n" for event in events]
        if not lines:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with journal_file_lock(self.path, timeout=self.lock_timeout):
            self._rotate_locked()
            with self.path.open("ab") as handle:
                for line in lines:
                    handle.write(line)
                handle.flush()
            self._rotate_locked()
        return len(lines)

    def _parse_lines(self, lines: Iterable[bytes | str]) -> list[dict[str, Any]]:
        events = []
        for line in lines:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = str(line).strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (TypeError, ValueError):
                continue
            event = normalize_stored_event(raw, max_bytes=self.event_bytes)
            if event is not None:
                events.append(event)
        return events

    def _tail_locked(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        # A live browser poll never copies the whole high-volume journal.
        # Search backwards only for the requested lines, with an explicit byte cap.
        with self.path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks = []
            newlines = 0
            remaining = min(position, MAX_READ_BYTES, (self.event_bytes + 1) * (limit + 1))
            while position > 0 and remaining > 0 and newlines <= limit:
                size = min(65536, position, remaining)
                position -= size
                remaining -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
            if position > 0:
                first = data.find(b"\n")
                data = data[first + 1:] if first >= 0 else b""
        return self._parse_lines(data.splitlines()[-limit:])

    def tail_snapshot(self, limit: int | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit or configured_recent_limit()), 2000))
        try:
            with journal_file_lock(self.path, timeout=self.lock_timeout):
                state = self._state_locked()
                return {
                    "events": self._tail_locked(limit),
                    "position": state["position"],
                    "generation": state["generation"],
                    "read_error": False,
                }
        except Exception:
            return {
                "events": [],
                "position": 0,
                "generation": None,
                "read_error": True,
            }

    def read_since(
        self,
        position: int,
        generation: str | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or configured_recent_limit()), 2000))
        position = max(0, int(position or 0))
        try:
            with journal_file_lock(self.path, timeout=self.lock_timeout):
                state = self._state_locked()
                current_generation = state["generation"]
                current_size = state["position"]
                if current_generation is None:
                    return {
                        "events": [],
                        "position": 0,
                        "generation": None,
                        "rotated": generation is not None,
                        "read_error": False,
                    }
                rotated = current_generation != generation or current_size < position
                if rotated:
                    return {
                        "events": self._tail_locked(limit),
                        "position": current_size,
                        "generation": current_generation,
                        "rotated": True,
                        "read_error": False,
                    }
                if current_size <= position:
                    return {
                        "events": [],
                        "position": position,
                        "generation": current_generation,
                        "rotated": False,
                        "read_error": False,
                    }
                lines = []
                next_position = position
                with self.path.open("rb") as handle:
                    handle.seek(position)
                    for _ in range(limit):
                        line = handle.readline(self.event_bytes + 2)
                        if handle.tell() - position > MAX_READ_BYTES:
                            break
                        if not line or not line.endswith(b"\n"):
                            break
                        next_position = handle.tell()
                        lines.append(line)
                if not lines:
                    return {
                        "events": [],
                        "position": position,
                        "generation": current_generation,
                        "rotated": False,
                        "read_error": False,
                    }
                return {
                    "events": self._parse_lines(lines),
                    "position": next_position,
                    "generation": current_generation,
                    "rotated": False,
                    "read_error": False,
                }
        except Exception:
            return {
                "events": [],
                "position": position,
                "generation": generation,
                "rotated": False,
                "read_error": True,
            }


class BoundedEventBuffer:
    def __init__(self, max_size: int):
        self.max_size = max(1, int(max_size))
        self._items: deque[tuple[int, dict[str, Any]]] = deque()
        self._condition = threading.Condition()
        self._unfinished = 0
        self._closed = False

    def put_nowait(self, priority: int, event: dict[str, Any]) -> tuple[bool, int]:
        with self._condition:
            if self._closed:
                return False, 0
            evicted = 0
            if len(self._items) >= self.max_size:
                victim = next(
                    (index for index, (current, _event) in enumerate(self._items) if current < priority),
                    None,
                )
                if victim is None:
                    return False, 0
                del self._items[victim]
                self._unfinished -= 1
                evicted = 1
            self._items.append((priority, event))
            self._unfinished += 1
            self._condition.notify()
            return True, evicted

    def get_batch(self, limit: int, timeout: float) -> list[dict[str, Any]]:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            batch = []
            while self._items and len(batch) < limit:
                _priority, event = self._items.popleft()
                batch.append(event)
            return batch

    def task_done(self, count: int) -> None:
        with self._condition:
            self._unfinished = max(0, self._unfinished - max(0, int(count)))
            if self._unfinished == 0:
                self._condition.notify_all()

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._unfinished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def size(self) -> int:
        with self._condition:
            return len(self._items)


def event_priority(event: dict[str, Any]) -> int:
    status = str(event.get("status") or "").lower()
    direction = str(event.get("direction") or "").lower()
    if status in {"error", "failed", "critical", "blocked"}:
        return 2
    if status in {"completed", "complete", "success", "ok"}:
        return 1
    if status == "started" or direction == "request":
        return 0
    return 1


class TelemetryEmitter:
    def __init__(
        self,
        journal: JsonlTelemetryJournal,
        *,
        queue_size: int,
        batch_size: int,
        enabled: bool = True,
    ):
        self.journal = journal
        self.buffer = BoundedEventBuffer(queue_size)
        self.batch_size = max(1, int(batch_size))
        self.enabled = bool(enabled)
        self._metrics = Counter()
        self._metrics_lock = threading.Lock()
        self.writer_id = uuid.uuid4().hex
        self._sequence = 0
        self._emit_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stopping = threading.Event()

    def _increment(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[name] += amount

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="telemetry-jsonl-writer",
                    daemon=True,
                )
                self._thread.start()

    def emit(self, kind: Any, source: Any, target: Any, operation: Any, **optional: Any):
        # Only short memory operations are serialized; file I/O remains on the
        # existing writer. Sequence order then matches this producer's enqueue order.
        with self._emit_lock:
            return self._emit_locked(kind, source, target, operation, **optional)

    def _emit_locked(self, kind: Any, source: Any, target: Any, operation: Any, **optional: Any):
        if not self.enabled:
            return None
        self._sequence += 1
        with self._metrics_lock:
            optional.update(writer_id=self.writer_id, sequence=self._sequence,
                            dropped_total=self._metrics["dropped"], invalid_total=self._metrics["invalid"],
                            sink_errors_total=self._metrics["sink_errors"])
        try:
            event = build_event(
                kind,
                source,
                target,
                operation,
                max_bytes=self.journal.event_bytes,
                **optional,
            )
        except (TelemetryContractError, TypeError, ValueError, OverflowError):
            self._increment("invalid")
            return None
        accepted, evicted = self.buffer.put_nowait(event_priority(event), event)
        if evicted:
            self._increment("dropped", evicted)
            self._increment("evicted_low_priority", evicted)
        if not accepted:
            self._increment("dropped")
            return None
        self._increment("emitted")
        self._ensure_thread()
        return event

    def _run(self) -> None:
        while not self._stopping.is_set() or self.buffer.size():
            batch = self.buffer.get_batch(self.batch_size, 0.2)
            if not batch:
                continue
            try:
                written = self.journal.append_batch(batch)
                self._increment("written", written)
                if written < len(batch):
                    self._increment("dropped", len(batch) - written)
            except Exception:
                self._increment("sink_errors")
                self._increment("dropped", len(batch))
            finally:
                self.buffer.task_done(len(batch))

    def flush(self, timeout: float = 1.0) -> bool:
        return self.buffer.flush(timeout)

    def close(self, timeout: float = 1.0) -> bool:
        self._stopping.set()
        self.buffer.close()
        drained = self.flush(timeout)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, timeout))
        return drained

    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            result = dict(self._metrics)
        for key in (
            "emitted",
            "written",
            "dropped",
            "evicted_low_priority",
            "invalid",
            "sink_errors",
        ):
            result.setdefault(key, 0)
        result["queued"] = self.buffer.size()
        result["queue_limit"] = self.buffer.max_size
        result["writer_id"] = self.writer_id
        result["sequence"] = self._sequence
        return result


def default_emitter() -> TelemetryEmitter:
    """Return a lazy singleton keyed by safe, post-dotenv configuration."""

    path = configured_path()
    max_bytes = configured_max_bytes()
    event_bytes = configured_event_max_bytes()
    queue_size = configured_queue_size()
    key = (str(path), max_bytes, event_bytes, queue_size)
    with _EMITTER_LOCK:
        emitter = _EMITTERS.get(key)
        if emitter is None:
            emitter = TelemetryEmitter(
                JsonlTelemetryJournal(
                    path,
                    max_bytes=max_bytes,
                    event_bytes=event_bytes,
                ),
                queue_size=queue_size,
                batch_size=configured_batch_size(),
                enabled=_env_flag("TELEMETRY_ENABLED", True),
            )
            _EMITTERS[key] = emitter
        return emitter


def close_emitters(timeout: float = 0.75) -> None:
    with _EMITTER_LOCK:
        emitters = list(_EMITTERS.values())
    for emitter in emitters:
        with contextlib.suppress(Exception):
            emitter.close(timeout)


def reset_emitters_for_tests() -> None:
    close_emitters(0.2)
    with _EMITTER_LOCK:
        _EMITTERS.clear()


atexit.register(close_emitters)
