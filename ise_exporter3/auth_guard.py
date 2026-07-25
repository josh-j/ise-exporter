"""Cross-process authentication-failure guard shared by ISE transports.

Ported unchanged from v2. It persists only an identity hash, a failure count and
a bounded deadline -- never credential material -- so an exporter restart or a
separate operator process cannot reset an account's backoff and walk ISE into a
lockout. Changing the configured user or host starts an independent guard.
"""
import fcntl
import hashlib
import math
import os
import stat
import threading


MAX_AUTH_BACKOFF_SECONDS = 86400


class PersistentAuthGuard:
    """Persist an identity-scoped failure count and bounded wall-clock deadline."""

    def __init__(self, path, identity, description="authentication"):
        self.path = str(path or "")
        material = "\0".join(str(value) for value in identity).encode("utf-8")
        self.identity = hashlib.sha256(material).hexdigest()[:16]
        self.description = str(description or "authentication")
        self._lock = threading.RLock()
        self._memory = (0, 0.0)

    def _open(self):
        path = os.path.abspath(os.path.expanduser(self.path))
        os.makedirs(os.path.dirname(path), exist_ok=True, mode=0o750)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o660,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"{self.description} guard is not a regular file")
            if metadata.st_size > 128:
                raise OSError(f"{self.description} guard exceeds 128 bytes")
            if metadata.st_uid == os.geteuid():
                parent_group = os.stat(os.path.dirname(path)).st_gid
                os.fchown(descriptor, -1, parent_group)
                os.fchmod(descriptor, 0o660)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _read(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 128).decode("ascii").strip()
        if not raw:
            return 0, 0.0
        fields = raw.split()
        if len(fields) != 4 or fields[0] != "v1" or fields[1] != self.identity:
            return 0, 0.0
        failures = int(fields[2])
        deadline = float(fields[3])
        if (failures < 0 or failures > 1_000_000
                or not math.isfinite(deadline) or deadline < 0):
            raise ValueError(f"invalid {self.description} guard state")
        return failures, deadline

    def _write(self, descriptor, failures, deadline):
        value = f"v1 {self.identity} {failures} {deadline:.6f}\n".encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, value)
        os.fsync(descriptor)

    def _update(self, callback):
        with self._lock:
            if not self.path:
                self._memory, result = callback(*self._memory)
                return result
            descriptor = self._open()
            try:
                current = self._read(descriptor)
                state, result = callback(*current)
                if state != current:
                    self._write(descriptor, *state)
                return result
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def blocked(self, now):
        def update(failures, deadline):
            deadline = min(deadline, now + MAX_AUTH_BACKOFF_SECONDS)
            return (failures, deadline), deadline > now
        return self._update(update)

    def failure(self, threshold, backoff, now):
        def update(failures, deadline):
            failures += 1
            deadline = min(deadline, now + MAX_AUTH_BACKOFF_SECONDS)
            if failures >= threshold and backoff:
                deadline = max(deadline, now + min(
                    backoff, MAX_AUTH_BACKOFF_SECONDS))
            return (failures, deadline), (failures, deadline)
        return self._update(update)

    def success(self):
        return self._update(lambda failures, deadline: (
            (0, 0.0), bool(failures or deadline)))
