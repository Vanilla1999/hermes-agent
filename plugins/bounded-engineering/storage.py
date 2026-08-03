"""Durable content-addressed storage for bounded-engineering records."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ImmutableStorageError(RuntimeError):
    """Raised when an immutable record cannot be written or verified safely."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def store_immutable_json(hermes_home: Path, payload: bytes, *, _namespace: str = "specs") -> Path:
    """Atomically persist exact canonical JSON bytes under their SHA-256 name."""
    if not isinstance(payload, bytes):
        raise ImmutableStorageError("payload must be bytes")
    digest = hashlib.sha256(payload).hexdigest()
    directory = Path(hermes_home) / "engineering" / _namespace
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    if target.exists():
        if target.read_bytes() != payload:
            raise ImmutableStorageError(f"digest collision or corrupt immutable record: {target}")
        os.chmod(target, 0o600)
        return target
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=directory)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temp, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ImmutableStorageError(f"digest collision or corrupt immutable record: {target}")
        _fsync_directory(directory)
        return target
    finally:
        temp.unlink(missing_ok=True)


def store_immutable_completion_evidence(hermes_home: Path, payload: bytes) -> Path:
    """Atomically persist canonical aggregate evidence for gated completion."""
    return store_immutable_json(hermes_home, payload, _namespace="completion-evidence")


def store_immutable_evidence(hermes_home: Path, payload: bytes) -> Path:
    """Atomically persist exact canonical evidence bytes under their SHA-256 name."""
    return store_immutable_json(hermes_home, payload, _namespace="evidence")
