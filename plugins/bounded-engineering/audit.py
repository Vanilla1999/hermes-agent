"""Bounded worker observability without retaining raw tool data."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    except Exception:
        payload = repr(type(value)).encode()
    return hashlib.sha256(payload).hexdigest()


def append_audit(hermes_home: Path, *, task_id: str, run_id: int, event: str, fields: dict[str, Any]) -> Path:
    """Append a bounded JSONL observation; callers must treat failures as non-fatal."""
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "fields": {name: _digest(value) for name, value in sorted(fields.items())},
        "run_id": run_id,
        "task_id": task_id,
    }
    target = hermes_home / "engineering" / "audit" / f"{task_id}-{run_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return target
