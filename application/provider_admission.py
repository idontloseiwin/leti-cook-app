"""Installation-wide provider admission on stable file locks.

No provider, SQL, config discovery or background thread. A caller owns each
returned permit until its single HTTP request has finished. Native deployment
uses stable flock inodes; tests may inject a process-local lock simulator.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
import random
from pathlib import Path
import stat
import threading
import time
import uuid
import provider_adaptive

try:
    import fcntl
except ImportError:
    fcntl = None

DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 50
POLL_SECONDS = 0.1
MAX_WAIT_SECONDS = 60
MAX_STATE_BYTES = 131072
KINDS = ("auto", "operator", "interactive")
FORMAT = 2


class AdmissionUnavailable(RuntimeError):
    """Fixed, source-free failure; no fallback to private/process-local slots."""


class AdmissionFull(AdmissionUnavailable):
    pass


class AdmissionTimeout(AdmissionUnavailable):
    pass


class AdmissionCancelled(AdmissionUnavailable):
    pass


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("provider_admission_identity_invalid")
    try:
        valid = str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("provider_admission_identity_invalid")
    return value


def _number(value, *, minimum=0, maximum=1e15):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum)


@dataclass(frozen=True)
class AdmissionConfig:
    installation_id: str
    generation: str
    boot_id: str
    concurrency: int = DEFAULT_CONCURRENCY

    def __post_init__(self):
        for value in (self.installation_id, self.generation, self.boot_id):
            _uuid(value)
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError("provider_admission_concurrency_invalid")

    @property
    def ticket_limit(self):
        # Includes active permits, not only waiting callers.
        return 2 * self.concurrency


class _Flock:
    def __init__(self):
        if fcntl is None:
            raise AdmissionUnavailable("provider_admission_flock_required")

    def acquire(self, fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def release(self, fd):
        # close(), not LOCK_UN: an inherited fd must not unlock its parent's
        # open-file description. This prototype requires spawn/exec children.
        pass


class _Handle:
    def __init__(self, fd, locks):
        self.fd, self.locks, self.held = fd, locks, False

    def acquire(self):
        self.held = self.locks.acquire(self.fd)
        return self.held

    def close(self):
        if self.fd is not None:
            try:
                if self.held:
                    self.locks.release(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None


def _identity(info):
    return [info.st_dev, info.st_ino]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class Permit:
    """One already admitted request; never transferable between processes."""
    def __init__(self, gate, identity, ticket, slot, epoch):
        self._gate, self._identity = gate, identity
        self._ticket, self._slot = ticket, slot
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._epoch, self._reported = epoch, False

    def observe(self, *, success=False, rate_seconds=None):
        """Record at most one response while this original permit is owned."""
        with self._lock:
            if self._slot is None or os.getpid() != self._pid or self._reported:
                raise AdmissionUnavailable('provider_admission_outcome_invalid')
            self._reported = True  # An uncertain state write must not be replayed.
            self._gate._observe(self._identity, self._epoch,
                                success=success, rate_seconds=rate_seconds)

    def close(self):
        with self._lock:
            if self._slot is None:
                return
            if os.getpid() != self._pid:
                raise AdmissionUnavailable("provider_admission_process_changed")
            try:
                self._gate._forget(self._identity)
            finally:
                # Actual slot is released before the liveness lock. No TTL
                # ever releases an active request on behalf of a live caller.
                self._slot.close()
                self._ticket.close()
                self._slot = self._ticket = None
                self._gate._notify()

    def __enter__(self):
        if self._slot is None or os.getpid() != self._pid:
            raise AdmissionUnavailable("provider_admission_permit_invalid")
        return self

    def __exit__(self, *exc):
        self.close()


class AdmissionGate:
    """Explicitly initialized, bounded installation-wide file protocol.

    initialize() is the only creator. Opening an existing gate never repairs or
    replaces a missing lock/state. A config or boot mismatch refuses admission.
    """
    def __init__(self, directory, config, *, _locks=None, _clock=time.monotonic):
        if not isinstance(config, AdmissionConfig):
            raise ValueError("provider_admission_config_invalid")
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("provider_admission_directory_invalid")
        self.config = config
        self._locks = _Flock() if _locks is None else _locks
        self._clock = _clock
        self._pid = os.getpid()
        self._condition = threading.Condition()
        self._directory_identity = None

    @property
    def _names(self):
        return (["guard.lock"]
                + [f"slot-{i}.lock" for i in range(self.config.concurrency)]
                + [f"ticket-{i}.lock" for i in range(self.config.ticket_limit)])

    def _directory(self):
        if os.getpid() != self._pid:
            raise AdmissionUnavailable("provider_admission_process_changed")
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise AdmissionUnavailable("provider_admission_directory_invalid")
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise AdmissionUnavailable("provider_admission_directory_invalid")
        if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise AdmissionUnavailable("provider_admission_directory_not_private")
        identity = _identity(info)
        if self._directory_identity is None:
            self._directory_identity = identity
        elif self._directory_identity != identity:
            raise AdmissionUnavailable("provider_admission_directory_changed")

    def _open(self, name, *, create=False, write=False, truncate=False):
        self._directory()
        path = self.directory / name
        if path.is_symlink():
            raise AdmissionUnavailable("provider_admission_file_invalid")
        flags = (os.O_RDWR if write or name.endswith(".lock") else os.O_RDONLY)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or _identity(info) != _identity(path.stat(follow_symlinks=False))
                    or (os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
                raise AdmissionUnavailable("provider_admission_file_invalid")
            if truncate:
                os.ftruncate(fd, 0)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _lock_file(self, name, files=None):
        handle = _Handle(self._open(name), self._locks)
        try:
            if files is not None and _identity(os.fstat(handle.fd)) != files[name]:
                raise AdmissionUnavailable("provider_admission_lock_changed")
            return handle
        except BaseException:
            handle.close()
            raise

    @classmethod
    def initialize(cls, directory, config, **options):
        gate = cls(directory, config, **options)
        try:
            # Explicit creator only; do not follow a symlinked parent.
            for path in (gate.directory, *gate.directory.parents):
                if path.is_symlink():
                    raise AdmissionUnavailable("provider_admission_directory_invalid")
            try:
                gate.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
                fresh_directory = True
            except FileExistsError:
                fresh_directory = False
            gate._directory()
            entries = {path.name for path in gate.directory.iterdir()}
            if not fresh_directory and "guard.lock" not in entries:
                raise AdmissionUnavailable("provider_admission_lock_missing")
            try:
                fd = gate._open("guard.lock", create=True)
                os.close(fd)
            except FileExistsError:
                pass
            with gate._guard(gate._now() + 5, None) as guard:
                if (gate.directory / "state.json").exists():
                    gate._load(guard)
                    return gate
                # Partial initialization is refused, not "repaired" by replacing
                # live inodes. A new isolated directory is recoverable explicitly.
                if (not fresh_directory
                        or set(path.name for path in gate.directory.iterdir()) != {"guard.lock"}):
                    raise AdmissionUnavailable("provider_admission_initialization_incomplete")
                files = {"guard.lock": _identity(os.fstat(guard.fd))}
                for name in gate._names[1:]:
                    fd = gate._open(name, create=True)
                    try:
                        files[name] = _identity(os.fstat(fd))
                    finally:
                        os.close(fd)
                state = {
                    "format": FORMAT, "installation_id": config.installation_id,
                    "generation": config.generation, "boot_id": config.boot_id,
                    "concurrency": config.concurrency, "effective": config.concurrency,
                    "cooldown_until": 0.0, "next_kind": 0, "sequence": 0,
                    "tickets": {}, "files": files, "adaptive": provider_adaptive.initial(),
                }
                gate._save(state)
            return gate
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError):
            raise AdmissionUnavailable("provider_admission_initialize_failed") from None

    @classmethod
    def for_backend(cls, directory, *, installation_id, boot_id, concurrency, **options):
        """Create or reopen at startup; reconfigure only after every caller drains.

        The backend already owns its installation runtime lock. The guard inode
        is also shared with surviving callers: a new backend never creates a
        second independent ceiling alongside an old request.
        """
        wanted = AdmissionConfig(installation_id, str(uuid.uuid4()), boot_id, concurrency)
        gate = cls(directory, wanted, **options)
        try:
            if not gate.directory.exists():
                return cls.initialize(directory, wanted, **options)
            gate._directory()
            fd = gate._open("state.json")
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise AdmissionUnavailable("provider_admission_state_invalid")
            identity = json.loads(raw, object_pairs_hook=_object)
            previous = AdmissionConfig(identity["installation_id"], identity["generation"],
                                       identity["boot_id"], identity["concurrency"])
            if previous.installation_id != installation_id:
                raise AdmissionUnavailable("provider_admission_installation_mismatch")
            gate = cls(directory, previous, **options)
            gate._directory()
            with gate._guard(gate._now() + 5, None) as guard:
                state = gate._load(guard)
                if previous.boot_id == boot_id and previous.concurrency == concurrency:
                    return gate
                handles = []
                try:
                    # Include stale/expired ticket slots as well as active HTTP
                    # slots. A metadata deadline is never evidence of exit.
                    for name in gate._names[1:]:
                        handle = gate._lock_file(name, state["files"])
                        handles.append(handle)
                        if not handle.acquire():
                            raise AdmissionUnavailable("provider_admission_previous_callers_active")
                    replacement = cls(directory, wanted, **options)
                    replacement._directory()
                    files = {"guard.lock": _identity(os.fstat(guard.fd))}
                    for name in replacement._names[1:]:
                        try:
                            fd = gate._open(name, create=True)
                        except FileExistsError:
                            fd = gate._open(name)
                        try:
                            files[name] = _identity(os.fstat(fd))
                        finally:
                            os.close(fd)
                    # Never delete or replace a slot/ticket inode, even when C
                    # shrinks. At most 50 slots and 100 tickets can ever exist.
                    replacement._save({
                        "format": FORMAT, "installation_id": wanted.installation_id,
                        "generation": wanted.generation, "boot_id": wanted.boot_id,
                        "concurrency": concurrency, "effective": concurrency,
                        "cooldown_until": 0.0, "next_kind": 0, "sequence": 0,
                        "tickets": {}, "files": files, "adaptive": provider_adaptive.initial(),
                    })
                    return replacement
                finally:
                    for handle in reversed(handles):
                        handle.close()
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise AdmissionUnavailable("provider_admission_startup_failed") from None

    @classmethod
    def join_existing(cls, directory, *, installation_id, boot_id, **options):
        """Join the current backend gate without creating or reconfiguring it.

        Standalone owner commands have no inherited generation or concurrency.
        Read those only through the secured existing files under their guard;
        every later acquire revalidates this captured configuration.
        """
        try:
            # Only the expected identities are known before reading. This local
            # bootstrap config is used solely to open the existing guard/state;
            # no slot access, admission or state write occurs before capture.
            gate = cls(directory, AdmissionConfig(installation_id, boot_id, boot_id), **options)
            with gate._guard(gate._now() + 5, None) as guard:
                fd = gate._open("state.json")
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read(MAX_STATE_BYTES + 1)
                if len(raw) > MAX_STATE_BYTES:
                    raise AdmissionUnavailable("provider_admission_state_invalid")
                identity = json.loads(raw, object_pairs_hook=_object)
                if type(identity) is not dict:
                    raise AdmissionUnavailable("provider_admission_state_invalid")
                captured = AdmissionConfig(identity["installation_id"], identity["generation"],
                                           identity["boot_id"], identity["concurrency"])
                if captured.installation_id != installation_id:
                    raise AdmissionUnavailable("provider_admission_installation_mismatch")
                if captured.boot_id != boot_id:
                    raise AdmissionUnavailable("provider_admission_boot_mismatch")
                gate.config = captured
                gate._load(guard)
            return gate
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise AdmissionUnavailable("provider_admission_join_failed") from None

    def _now(self):
        value = self._clock()
        if not _number(value):
            raise AdmissionUnavailable("provider_admission_clock_invalid")
        return value

    def _notify(self):
        with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _cancelled(cancel):
        if cancel is not None and cancel.is_set():
            raise AdmissionCancelled("provider_admission_cancelled")

    def _wait(self, deadline, cancel):
        self._cancelled(cancel)
        remaining = deadline - self._now()
        if remaining <= 0:
            raise AdmissionTimeout("provider_admission_timeout")
        with self._condition:
            self._condition.wait(min(POLL_SECONDS, remaining))
        self._cancelled(cancel)

    @contextmanager
    def _guard(self, deadline, cancel):
        handle = self._lock_file("guard.lock")
        try:
            while not handle.acquire():
                self._wait(deadline, cancel)
            self._cancelled(cancel)
            # A blocked caller's budget must not be reset after taking the lock.
            if self._now() >= deadline:
                raise AdmissionTimeout("provider_admission_timeout")
            if _identity(os.fstat(handle.fd)) != _identity((self.directory / "guard.lock").stat()):
                raise AdmissionUnavailable("provider_admission_lock_changed")
            yield handle
        finally:
            handle.close()

    def _load(self, guard):
        fd = self._open("state.json")
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise AdmissionUnavailable("provider_admission_state_invalid")
        state = json.loads(raw, object_pairs_hook=_object)
        required = {"format", "installation_id", "generation", "boot_id", "concurrency",
                    "effective", "cooldown_until", "next_kind", "sequence", "tickets", "files", "adaptive"}
        c = self.config
        if (type(state) is not dict or set(state) != required
                or type(state["format"]) is not int or state["format"] != FORMAT
                or state["installation_id"] != c.installation_id
                or state["generation"] != c.generation or state["boot_id"] != c.boot_id
                or type(state["concurrency"]) is not int or state["concurrency"] != c.concurrency
                or type(state["effective"]) is not int or not 1 <= state["effective"] <= c.concurrency
                or not _number(state["cooldown_until"])
                or type(state["next_kind"]) is not int or state["next_kind"] not in range(len(KINDS))
                or type(state["sequence"]) is not int or not 0 <= state["sequence"] < 2**53
                or type(state["tickets"]) is not dict or len(state["tickets"]) > c.ticket_limit
                or type(state["files"]) is not dict or set(state["files"]) != set(self._names)):
            raise AdmissionUnavailable("provider_admission_state_invalid")
        provider_adaptive.validate(state['adaptive'])
        for name, expected in state["files"].items():
            if (type(expected) is not list or len(expected) != 2
                    or any(type(v) is not int or v < 0 for v in expected)):
                raise AdmissionUnavailable("provider_admission_state_invalid")
            info = (self.directory / name).stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or _identity(info) != expected:
                raise AdmissionUnavailable("provider_admission_lock_changed")
        if _identity(os.fstat(guard.fd)) != state["files"]["guard.lock"]:
            raise AdmissionUnavailable("provider_admission_lock_changed")
        identities, sequences, slots = set(), set(), set()
        for index, row in state["tickets"].items():
            if (index not in {str(i) for i in range(c.ticket_limit)}
                    or type(row) is not dict
                    or set(row) != {"id", "kind", "sequence", "deadline", "slot"}
                    or row["kind"] not in KINDS or type(row["sequence"]) is not int
                    or not 1 <= row["sequence"] <= state["sequence"]
                    or not _number(row["deadline"])):
                raise AdmissionUnavailable("provider_admission_state_invalid")
            _uuid(row["id"])
            if row["id"] in identities or row["sequence"] in sequences:
                raise AdmissionUnavailable("provider_admission_state_invalid")
            identities.add(row["id"])
            sequences.add(row["sequence"])
            slot = row["slot"]
            if slot is not None:
                if type(slot) is not int or not 0 <= slot < c.concurrency or slot in slots:
                    raise AdmissionUnavailable("provider_admission_state_invalid")
                slots.add(slot)
        probe = state['adaptive']['probe']
        if probe is not None and not any(row['id'] == probe and row['slot'] is not None
                                         for row in state['tickets'].values()):
            raise AdmissionUnavailable('provider_admission_probe_invalid')
        return state

    def _save(self, state):
        raw = json.dumps(state, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_STATE_BYTES:
            raise AdmissionUnavailable("provider_admission_state_invalid")
        # One bounded staging file under guard; crash cannot grow temp files.
        path = self.directory / "state.next"
        try:
            fd = self._open("state.next", create=True, write=True)
        except FileExistsError:
            fd = self._open("state.next", write=True, truncate=True)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, self.directory / "state.json")
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _reap(self, state, now):
        changed = False
        for index, row in list(state["tickets"].items()):
            handle = self._lock_file(f"ticket-{index}.lock", state["files"])
            try:
                dead = handle.acquire()
            finally:
                handle.close()
            # Expiry removes only queued metadata. Its live OS lock still
            # prevents reuse; active request ownership never expires by clock.
            if dead or (row["slot"] is None and row["deadline"] <= now):
                provider_adaptive.forgotten(state, row['id'])
                del state["tickets"][index]
                changed = True
        return changed

    def _register(self, state, kind, deadline):
        if len(state["tickets"]) >= self.config.ticket_limit:
            raise AdmissionFull("provider_admission_full")
        if state["sequence"] >= 2**53 - 1:
            raise AdmissionUnavailable("provider_admission_sequence_exhausted")
        for index in range(self.config.ticket_limit):
            key = str(index)
            if key in state["tickets"]:
                continue
            handle = self._lock_file(f"ticket-{index}.lock", state["files"])
            try:
                acquired = handle.acquire()
            except BaseException:
                handle.close()
                raise
            if acquired:
                identity = str(uuid.uuid4())
                state["sequence"] += 1
                state["tickets"][key] = dict(id=identity, kind=kind, deadline=deadline,
                                             sequence=state["sequence"], slot=None)
                return key, identity, handle
            handle.close()
        raise AdmissionFull("provider_admission_full")

    @staticmethod
    def _head(state):
        waiting = [(index, row) for index, row in state["tickets"].items() if row["slot"] is None]
        for offset in range(len(KINDS)):
            kind = KINDS[(state["next_kind"] + offset) % len(KINDS)]
            choices = [(index, row) for index, row in waiting if row["kind"] == kind]
            if choices:
                return min(choices, key=lambda item: item[1]["sequence"])[0]
        return None

    def _slot(self, state):
        # A closer can release its kernel locks after this turn's _reap when
        # _forget could not obtain our guard. Do not reuse its still-recorded
        # slot until a later reap removes that ticket; duplicate metadata would
        # make every subsequent load fail closed.
        referenced = {row["slot"] for row in state["tickets"].values()
                      if row["slot"] is not None}
        free, busy, chosen = None, 0, None
        try:
            for index in range(self.config.concurrency):
                handle = self._lock_file(f"slot-{index}.lock", state["files"])
                try:
                    acquired = handle.acquire()
                except BaseException:
                    handle.close()
                    raise
                if acquired:
                    if free is None and index not in referenced:
                        free, chosen = handle, index
                    else:
                        handle.close()
                else:
                    busy += 1
                    handle.close()
            # Include occupied slots above a reduced effective limit.
            if busy < state["effective"]:
                result, free = free, None
                return chosen, result
            return None, None
        finally:
            if free is not None:
                free.close()

    def acquire(self, kind, *, timeout=5.0, cancel=None):
        if kind not in KINDS or not _number(timeout, minimum=0.001, maximum=MAX_WAIT_SECONDS):
            raise ValueError("provider_admission_request_invalid")
        deadline = self._now() + timeout
        ticket = slot = None
        identity = key = None
        try:
            while True:
                self._cancelled(cancel)
                with self._guard(deadline, cancel) as guard:
                    state = self._load(guard)
                    now = self._now()
                    changed = self._reap(state, now)
                    if ticket is None:
                        key, identity, ticket = self._register(state, kind, deadline)
                        changed = True
                    if key not in state["tickets"] or state["tickets"][key]["id"] != identity:
                        raise AdmissionTimeout("provider_admission_timeout")
                    if provider_adaptive.may_admit(state, now) and self._head(state) == key:
                        slot_index, slot = self._slot(state)
                        if slot is not None:
                            state["tickets"][key]["slot"] = slot_index
                            state["next_kind"] = (KINDS.index(kind) + 1) % len(KINDS)
                            epoch = provider_adaptive.admitted(state, identity)
                            changed = True
                    self._cancelled(cancel)
                    if changed:
                        self._save(state)
                if slot is not None:
                    self._cancelled(cancel)
                    if self._now() >= deadline:
                        raise AdmissionTimeout("provider_admission_timeout")
                    permit = Permit(self, identity, ticket, slot, epoch)
                    ticket = slot = None
                    return permit
                self._wait(deadline, cancel)
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise AdmissionUnavailable("provider_admission_unavailable") from None
        finally:
            if slot is not None:
                slot.close()
            if ticket is not None:
                ticket.close()
                self._notify()
            # Stale metadata is reclaimed under the next successful guard.
            # No best-effort write can convert failure into permission to send.

    def _forget(self, identity):
        try:
            with self._guard(self._now() + POLL_SECONDS, None) as guard:
                state = self._load(guard)
                provider_adaptive.forgotten(state, identity)
                state["tickets"] = {key: row for key, row in state["tickets"].items()
                                    if row["id"] != identity}
                self._save(state)
        except (AdmissionUnavailable, OSError, ValueError, TypeError, KeyError):
            # Kernel locks are authoritative; close() always releases them.
            # Invalid state remains fail-closed, never silently reinitialized.
            pass

    def _observe(self, identity, epoch, *, success, rate_seconds):
        try:
            with self._guard(self._now() + 5, None) as guard:
                state = self._load(guard)
                if not any(row['id'] == identity and row['slot'] is not None
                           for row in state['tickets'].values()):
                    raise AdmissionUnavailable('provider_admission_outcome_invalid')
                changed = provider_adaptive.observe(state, identity, epoch,
                    now=self._now(), success=success, rate_seconds=rate_seconds, jitter=random.random())
                if changed:
                    self._save(state)
            self._notify()
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise AdmissionUnavailable('provider_admission_unavailable') from None

    def set_cooldown(self, seconds, *, effective=None):
        """Prototype control hook; caller owns adaptive policy, not this gate."""
        if (not _number(seconds, minimum=0.001, maximum=60)
                or (effective is not None and (type(effective) is not int
                    or not 1 <= effective <= self.config.concurrency))):
            raise ValueError("provider_admission_cooldown_invalid")
        try:
            with self._guard(self._now() + 5, None) as guard:
                state = self._load(guard)
                state["cooldown_until"] = max(state["cooldown_until"], self._now() + seconds)
                if effective is not None:
                    # Restoration needs a separately reviewed adaptive owner.
                    state["effective"] = min(state["effective"], effective)
                self._save(state)
            self._notify()
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise AdmissionUnavailable("provider_admission_unavailable") from None

    def snapshot(self):
        """Counts only, no ticket/config identities. Also reaps dead waiters."""
        try:
            with self._guard(self._now() + 5, None) as guard:
                state = self._load(guard)
                if self._reap(state, self._now()):
                    self._save(state)
                active = sum(row["slot"] is not None for row in state["tickets"].values())
                return dict(concurrency=self.config.concurrency, effective=state["effective"],
                            tickets=len(state["tickets"]), active=active,
                            waiting=len(state["tickets"]) - active,
                            cooling_down=self._now() < state["cooldown_until"])
        except AdmissionUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise AdmissionUnavailable("provider_admission_unavailable") from None
