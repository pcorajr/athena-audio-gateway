"""Private append-only transcript; shares .transcript.lock with the gateway."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import sys
import time


class Transcript:
    def __init__(self, root=None, *, clock=None):
        self.root = Path(root or os.environ.get("ATHENA_RADIO_TRANSCRIPT_DIR") or
                         Path.home() / ".hermes/cache/athena-radio")
        self.clock = clock or (lambda: datetime.now().astimezone())

    def _open(self, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            os.close(fd)
            raise ValueError("transcript_file_not_private")
        return fd

    @contextmanager
    def _lock(self):
        fd = self._open(self.root / ".transcript.lock")
        locked = False
        try:
            deadline = time.monotonic() + 1
            while not locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def log(self, *, kind, request=None, text="", outcome="", route="LLM", **metadata):
        record = dict(kind=kind, text=text, outcome=outcome, route=route, **metadata)
        if request is not None:
            record.update(transmission_id=request.transmission_id, speaker=request.speaker,
                          frequency_hz=request.frequency_hz, modulation=request.modulation)
        try:
            self.append(record)
            return True
        except (OSError, ValueError, TypeError):
            # Never include exception bodies: provider errors may contain secrets.
            print("athena_radio_transcript_write_failed", file=sys.stderr, flush=True)
            return False

    def append(self, record):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise ValueError("transcript_directory_not_private")
        with self._lock():
            now = self.clock().astimezone()
            record = dict(record, time=now.isoformat())
            raw = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
            def line(value):
                return "".join(c if c >= " " and c not in "\x7f\u2028\u2029" else " " for c in str(value))
            text = (f"{record['time']} {record.get('frequency_hz', 0)/1000000:.3f} "
                    f"{line(record.get('modulation', ''))} {line(record.get('speaker', 'Athena'))} "
                    f"[{line(record['kind'])} {line(record.get('transmission_id', ''))} "
                    f"{line(record.get('route', ''))} {line(record.get('outcome', ''))}] {line(record.get('text', ''))}\n")
            for suffix, content in (("jsonl", raw), ("txt", text)):
                fd = self._open(self.root / f"transcript-{now:%Y%m%d}.{suffix}")
                try:
                    data = content.encode("utf-8")
                    if os.write(fd, data) != len(data):
                        raise OSError("short_transcript_append")
                finally:
                    os.close(fd)
