"""Deterministic assembly of a bounded-engineering task creation record."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .body import render_compact_body
    from .contract import Contract, load_contract
    from .spec import CanonicalSpec, canonicalize_spec
    from .storage import store_immutable_json
else:  # Direct-module tests and scripts.
    from body import render_compact_body
    from contract import Contract, load_contract
    from spec import CanonicalSpec, canonicalize_spec
    from storage import store_immutable_json


@dataclass(frozen=True)
class PreparedTaskRecord:
    contract: Contract
    contract_sha256: str
    spec: CanonicalSpec
    spec_path: Path
    body: str


def prepare_task_record(
    *, repo: Path, hermes_home: Path, spec_raw: Any, baseline_head: str
) -> PreparedTaskRecord:
    """Validate and persist all authoritative inputs before a Kanban mutation."""
    contract = load_contract(repo)
    spec = canonicalize_spec(
        spec_raw,
        verification_ids={item.id for item in contract.verification},
    )
    spec_path = store_immutable_json(hermes_home, spec.canonical_json)
    acceptance = tuple((item["id"], item["text"]) for item in spec_raw["acceptance"])
    body = render_compact_body(
        spec_sha256=spec.sha256,
        contract_sha256=contract.sha256,
        baseline_head=baseline_head,
        repository=str(Path(repo).resolve()),
        objective=spec_raw["objective"],
        acceptance=acceptance,
        allowed_paths=(*spec.allowed_roots, *spec.allowed_files),
        verification_ids=tuple(spec_raw["required_verification_ids"]),
        risk=spec_raw["risk"],
    )
    return PreparedTaskRecord(contract, contract.sha256, spec, spec_path, body)
