"""Optional Eye → mission memory link; no DCS connection and no model dependency.

Developer rule: every new Eye fact = publish an observation.
Use publish(type, payload) at the derivation seam. Unknown types are stored
unchanged by memory. Raw Eye inputs are claims, never the independent raw ledger.
All socket I/O runs on the publisher thread except explicitly bounded queries.
"""
from collections import deque
import copy
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

SCHEMA_VERSION = 1
MAX_FRAME = 1024 * 1024


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
    def __init__(self, eye=None, *, path=None, capacity=512, start=True):
        if capacity < 1:
            raise ValueError("positive queue capacity required")
        self.eye, self.path, self.capacity = eye, path, capacity
        self.queue = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.identity = None
        self.bindings = {}
        self.dropped = 0
        self.sent = 0
        self.failures = 0
        self._last = {}
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, name="eye-memory", daemon=True)
            self.thread.start()

    def set_identity(self, session_id, filename, source):
        identity = None if session_id is None else (int(session_id), str(filename), str(source))
        with self.lock:
            if self.identity != identity:
                self._last.clear()
            self.identity = identity
        self.wake.set()

    @property
    def mission_key(self):
        with self.lock:
            return self.bindings.get(self.identity)

    def publish(self, kind, payload, *, at=None):
        """Bounded local enqueue only; no filesystem or socket work on the Eye."""
        try:
            row = {"schema_version": SCHEMA_VERSION, "id": str(uuid.uuid4()),
                   "time": time.time() if at is None else at, "type": str(kind),
                   "payload": copy.deepcopy(payload)}
            with self.lock:
                if self.identity is None:
                    self.dropped += 1
                    return False
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

    def capture_picture(self, pic):
        own = pic.get("ownship") or {}
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
            # Keep full evidence; changes include freshness and per-shot results.
            token = json.dumps(payload, sort_keys=True, default=str)
            with self.lock:
                changed = self._last.get(kind) != token
                self._last[kind] = token
            if changed:
                self.publish(kind, {"value": payload})

    def _send_one(self):
        with self.lock:
            if not self.queue:
                return False
            identity, row = self.queue.popleft()
            key = self.bindings.get(identity)
        try:
            if key is None:
                reply = request({"operation": "resolve", "session_id": identity[0], "filename": identity[1],
                                 "source": identity[2]}, path=self.path)
                key = reply["mission"]["mission_key"]
                with self.lock:
                    self.bindings[identity] = key
                    # Bound long-running Eye state too, not only its queue.
                    while len(self.bindings) > 10:
                        del self.bindings[next(iter(self.bindings))]
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
            if not self._send_one():
                self.wake.clear()
                self.wake.wait(0.2)

    def summary(self):
        key = self.mission_key
        if key is None:
            return None
        try:
            return request({"operation": "summary", "mission": key}, path=self.path, timeout=0.12)["memory"]
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

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=0.3)


def attach(eye):
    if os.environ.get("ATHENA_MEMORY_ENABLED", "0") == "1":
        eye.memory = MemoryLink(eye)
    return eye
