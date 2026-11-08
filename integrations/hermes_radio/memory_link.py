"""Optional Eye → mission memory link; no DCS connection and no model dependency.

Developer rule: every new Eye fact = publish an observation.
Use publish(type, payload) at the derivation seam. Unknown types are stored
unchanged by memory. Raw Eye inputs are claims, never the independent raw ledger.
All socket I/O runs on the publisher thread except explicitly bounded queries.

Radio cumulative tally: mission_tally(owner="me", category="armor") sends
{"operation":"mission_tally","mission":"<resolved-key>","owner":"me","category":"armor"}
and returns the response's tally object: count, by_type, by_category, targets
(at most 100), targets_truncated, coverage. me|wingman|friendly|all; friendly
excludes me/wingman, all includes all sides. Categories include all, armor,
air_defense (sam+aaa), infantry, logistics, aircraft, ship, scenery, unknown.
The whole socket exchange has a 150 ms deadline; None means unavailable, NOT 0.
"""
from collections import deque
import copy
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
import uuid

SCHEMA_VERSION = 1
MAX_FRAME = 1024 * 1024
SNAPSHOT_TYPES = frozenset({"cockpit", "cockpit_input", "life_damage", "fuel", "loadout"})
SNAPSHOT_INTERVAL = 10.0
DISCONNECT_GRACE = 60.0


def _semantic(value):
    """Ignore sampling metadata for comparison, never strip stored evidence."""
    if isinstance(value, dict):
        return {k: _semantic(v) for k, v in value.items()
                if k not in {"t", "time", "at", "timestamp", "packets", "window_s"}
                and not k.endswith("age_s")}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def _critical_fields(value, prefix=()):
    """Sparse packets omit slow fields: absence is not a gear/stores change."""
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = prefix + (key,)
            if item is None:
                continue  # sparse derived payloads use null between slow updates
            if key in {"gear", "pay", "payload", "loadout", "stores", "ammo", "weapons", "gun_rounds",
                       "life", "health", "life_percent", "damage"} and not isinstance(item, dict):
                result[path] = item
            elif key in {"pay", "payload", "loadout", "stores"}:
                result[path] = item
            else:
                result.update(_critical_fields(item, path))
    return result


def socket_path():
    root = os.environ.get("ATHENA_MEMORY_ROOT", "~/.hermes/cache/athena-memory")
    return Path(os.environ.get("ATHENA_MEMORY_SOCKET", str(Path(root).expanduser() / "memory.sock"))).expanduser()


def request(payload, *, path=None, timeout=0.12):
    """One whole-exchange monotonic budget, not one timeout per recv chunk."""
    deadline = time.monotonic() + timeout
    data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ValueError("memory frame too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        def budget():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("memory deadline")
            conn.settimeout(left)
        budget()
        conn.connect(str(path or socket_path()))
        budget()
        conn.sendall(data)
        chunks = bytearray()
        while b"\n" not in chunks:
            budget()
            part = conn.recv(min(65536, MAX_FRAME + 1 - len(chunks)))
            if not part:
                raise OSError("memory closed without response")
            chunks.extend(part)
            if len(chunks) > MAX_FRAME:
                raise ValueError("memory response too large")
        reply = json.loads(chunks)
        if time.monotonic() >= deadline:
            raise TimeoutError("memory deadline")
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise ValueError("memory request failed")
        return reply


class MemoryLink:
    def __init__(self, eye=None, *, path=None, capacity=512, start=True, clock=None):
        if capacity < 1:
            raise ValueError("positive queue capacity required")
        self.eye, self.path, self.capacity = eye, path, capacity
        self.queue = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.identity = None
        self.generation = 0
        self.bindings = {}
        self.dropped = 0
        self.sent = 0
        self.failures = 0
        self._last = {}
        self.clock = clock or time.monotonic
        self.sampled_out = 0
        self._samples = {}
        self._active = set()
        self._pending_debriefs = {}
        self._auto_debriefed = set()
        self._disconnected_at = None
        self.thread = None
        self.debrief_thread = None
        self.debrief_status = None
        if start:
            self.thread = threading.Thread(target=self._run, name="eye-memory", daemon=True)
            self.thread.start()

    def invalidate(self):
        """Fence snapshots/in-flight metadata; queued facts keep their identity."""
        with self.lock:
            self._end_mission_locked("unload")
            self.generation += 1
            self.identity = None
            self._last.clear()
            self._samples.clear()
            self._disconnected_at = None
            return self.generation

    def set_identity(self, session_id, filename, source, *, generation=None):
        identity = None if session_id is None else (int(session_id), str(filename), str(source))
        with self.lock:
            if generation is not None and generation != self.generation:
                return False
            if self.identity != identity:
                self._end_mission_locked("mission_change" if identity else "unload")
                self._last.clear()
                self._samples.clear()
                self._disconnected_at = None
                if generation is None:
                    self.generation += 1
            self.identity = identity
        self.wake.set()
        return True

    @property
    def mission_key(self):
        with self.lock:
            return self.bindings.get(self.identity)

    def _end_mission_locked(self, reason):
        if self.identity in self._active:
            self._pending_debriefs.setdefault(self.identity, reason)

    def _sample_locked(self, kind, payload):
        semantic = _semantic(payload)
        token = json.dumps(semantic, sort_keys=True, default=str)
        now = self.clock()
        previous = self._samples.get(kind)
        critical = _critical_fields(semantic)
        urgent = False
        if previous is not None:
            old_token, last_sent, old_fields = previous
            for path, value in critical.items():
                if path not in old_fields:
                    urgent = True  # first available gear/stores/life evidence
                    continue
                if value == old_fields[path]:
                    continue
                before = old_fields[path]
                if path[-1] in {"life", "health", "life_percent"}:
                    urgent |= (isinstance(value, (int, float)) and isinstance(before, (int, float))
                               and value < before)
                else:
                    urgent = True
            old_fields.update(critical)
            critical = old_fields
            if not urgent and (token == old_token or now - last_sent < SNAPSHOT_INTERVAL):
                self.sampled_out += 1
                return False
        self._samples[kind] = (token, now, critical)
        return True

    def publish(self, kind, payload, *, at=None, generation=None, _activity=True):
        """Bounded local enqueue only; no filesystem or socket work on the Eye.

        High-rate snapshots are on-change, at least ten monotonic seconds apart,
        except life drops and gear/stores changes. False can mean sampled out
        (sampled_out counter), not just lost (dropped counter). Event types are
        never sampled. The existing bounded queue can still lose rows on outage.
        """
        try:
            row = {"schema_version": SCHEMA_VERSION, "id": str(uuid.uuid4()),
                   "time": time.time() if at is None else at, "type": str(kind),
                   "payload": copy.deepcopy(payload)}
            with self.lock:
                if self.identity is None or (generation is not None and generation != self.generation):
                    self.dropped += 1
                    return False
                if _activity:
                    self._active.add(self.identity)
                if kind in SNAPSHOT_TYPES and not self._sample_locked(kind, row["payload"]):
                    return False
                if kind == "mission_end" or (kind == "event" and payload.get("kind") == "mission_end"):
                    self._end_mission_locked("mission_end")
                if len(self.queue) >= self.capacity:
                    self.queue.popleft()
                    self.dropped += 1
                self.queue.append((self.identity, row))
            self.wake.set()
            return True
        except Exception:  # observation enrichment must not break a radio turn
            with self.lock:
                self.dropped += 1
            return False

    def capture_picture(self, pic, *, at=None):
        generation = pic.get("_memory_generation", self.generation)
        own = pic.get("ownship") or {}
        with self.lock:
            if self.identity is None or generation != self.generation:
                self.dropped += 1
                return
            if pic.get("connected") is True:
                self._disconnected_at = None
                if own:
                    self._active.add(self.identity)
            elif pic.get("connected") is False and self.identity in self._active:
                now = self.clock()
                if self._disconnected_at is None:
                    self._disconnected_at = now
                elif now - self._disconnected_at > DISCONNECT_GRACE:
                    self._end_mission_locked("disconnect")
        facts = {"loadout": own.get("loadout"), "fuel": own.get("fuel"),
                 "life_damage": {k: own[k] for k in ("life", "life0", "damage", "health") if k in own},
                 "cockpit": pic.get("cockpit"), "sam_threat": pic.get("threats"),
                 "weapons_used": pic.get("my_weapons_used_this_mission"),
                 "shots": pic.get("my_shots_last_15_min"),
                 "kill_timeline": pic.get("my_kills_this_mission"),
                 "loss_timeline": pic.get("recent_kills_and_losses_last_10_min")}
        for kind, payload in facts.items():
            if payload is None:
                continue
            if kind in SNAPSHOT_TYPES:
                self.publish(kind, {"value": payload}, generation=generation, at=at, _activity=False)
                continue
            # Keep full event-derived evidence, including per-shot results.
            token = json.dumps(payload, sort_keys=True, default=str)
            with self.lock:
                if self.identity is None or generation != self.generation:
                    self.dropped += 1
                    continue
                changed = self._last.get(kind) != token
                self._last[kind] = token
            if changed:
                self.publish(kind, {"value": payload}, generation=generation, at=at, _activity=False)

    def _auto_debrief_one(self):
        """Sender-only: drain evidence before one automatic attempt per mission key.

        Failed/ambiguous receipts remain visible; manual regeneration is allowed.
        This is process-local deduplication, not a durable exactly-once protocol.
        """
        candidate = None
        with self.lock:
            for identity, reason in list(self._pending_debriefs.items()):
                key = self.bindings.get(identity)
                if key in self._auto_debriefed:
                    del self._pending_debriefs[identity]
                    continue
                if any(queued_identity == identity for queued_identity, _ in self.queue):
                    continue
                candidate = (identity, reason)
                break
        if candidate is None:
            return False
        identity, reason = candidate
        try:
            # Overflow may have lost all queued Eye rows; the raw archive can
            # still identify and report this mission. Resolve, never guess.
            key = self._resolve_identity(identity)
        except (OSError, ValueError, KeyError, TimeoutError):
            self.failures += 1
            return False
        with self.lock:
            if key in self._auto_debriefed:
                self._pending_debriefs.pop(identity, None)
                return False
            if any(queued_identity == identity for queued_identity, _ in self.queue):
                return False
            if not self._schedule_debrief_locked(key, reason=reason):
                return False
            self._auto_debriefed.add(key)
            self._pending_debriefs.pop(identity, None)
            return True

    def _resolve_identity(self, identity):
        with self.lock:
            key = self.bindings.get(identity)
        if key is None:
            reply = request({"operation": "resolve", "session_id": identity[0], "filename": identity[1],
                             "source": identity[2]}, path=self.path)
            key = reply["mission"]["mission_key"]
            with self.lock:
                self.bindings[identity] = key
                while len(self.bindings) > 10:
                    del self.bindings[next(iter(self.bindings))]
        return key

    def _send_one(self):
        with self.lock:
            if not self.queue:
                return False
            identity, row = self.queue.popleft()
        try:
            key = self._resolve_identity(identity)
            row["mission_key"] = key
            request({"operation": "observe", "observation": row}, path=self.path)
            self.sent += 1
            return True
        except (OSError, ValueError, KeyError, TimeoutError):
            self.failures += 1
            with self.lock:
                if len(self.queue) < self.capacity:
                    self.queue.appendleft((identity, row))
                else:
                    self.dropped += 1  # this in-flight item is the oldest
            return False

    def _run(self):
        next_picture = 0
        while not self.stop.is_set():
            if self.eye is not None and time.monotonic() >= next_picture:
                try:
                    self.capture_picture(self.eye.picture())
                except Exception:
                    self.failures += 1
                next_picture = time.monotonic() + 1
            sent = self._send_one()
            self._auto_debrief_one()
            if not sent:
                self.wake.clear()
                self.wake.wait(0.2)

    def summary(self):
        generation = self.generation
        key = self.mission_key
        if key is None:
            return None
        try:
            result = request({"operation": "summary", "mission": key}, path=self.path, timeout=0.12)["memory"]
            return result if generation == self.generation else None
        except (OSError, ValueError, KeyError, TimeoutError):
            return None

    def debrief(self):
        key = self.mission_key
        if key is None:
            return None
        try:
            return request({"operation": "debrief", "mission": key}, path=self.path, timeout=4)["path"]
        except (OSError, ValueError, KeyError, TimeoutError):
            return None

    def mission_tally(self, owner="me", category=None):
        generation = self.generation
        key = self.mission_key
        if key is None:
            return None
        try:
            result = request({"operation": "mission_tally", "mission": key,
                              "owner": owner, "category": category}, path=self.path, timeout=0.15)["tally"]
            return result if generation == self.generation else None
        except (OSError, ValueError, KeyError, TimeoutError):
            return None

    def schedule_debrief(self):
        """Accept a report job without holding a radio turn during disk/socket I/O.

        The requested short radio acknowledgement means queued, not a file receipt.
        Completion/failure is retained here and in the operator log.
        """
        with self.lock:
            key = self.bindings.get(self.identity)
            if key is None:
                return False
            return self._schedule_debrief_locked(key)

    def _schedule_debrief_locked(self, key, *, reason=None):
        if self.debrief_thread and self.debrief_thread.is_alive():
            return reason is None and (self.debrief_status or {}).get("mission_key") == key
        context = {"mission_key": key}
        if reason is not None:
            context["reason"] = reason
        self.debrief_status = dict(context, state="queued")

        def write():
            try:
                path = request({"operation": "debrief", "mission": key,
                                "automatic": reason is not None}, path=self.path, timeout=4)["path"]
                status = dict(context, state="written", path=path)
            except (OSError, ValueError, KeyError, TimeoutError) as error:
                status = dict(context, state="failed", error=type(error).__name__)
            with self.lock:
                self.debrief_status = status
            logging.getLogger(__name__).info("memory debrief: %s", status)
            self.wake.set()

        self.debrief_thread = threading.Thread(target=write, name="eye-debrief", daemon=True)
        self.debrief_thread.start()
        return True

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=0.3)


def attach(eye):
    if os.environ.get("ATHENA_MEMORY_ENABLED", "0") == "1":
        eye.memory = MemoryLink(eye)
    return eye


def source_matches():
    """Startup reuse check: query only the local socket, never a DCS endpoint."""
    from metis.state_service.memory_source import resolve_source
    try:
        source = resolve_source()
        health = request({"operation": "health"})
        return health.get("source") == source.name and health.get("endpoint") == source.target
    except (OSError, ValueError, KeyError, TimeoutError):
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Check optional local memory source before Eye startup")
    parser.add_argument("--check-source", action="store_true", required=True)
    parser.parse_args()
    raise SystemExit(0 if source_matches() else 1)
