"""One-card, token-bounded planning for the native Kanban control plane."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "low"
DEFAULT_PROMPT_BYTES = 12_288
MAX_RESPONSE_BYTES = 65_536


class PlanError(RuntimeError):
    """A plan could not be generated or proved safe."""


@dataclass(frozen=True)
class CanonicalPlan:
    canonical_json: bytes
    sha256: str
    task_sha256: str
    task: dict[str, Any]
    board_revision: str


def board_revision(tasks: Collection[Any]) -> str:
    """Hash only durable scheduling fields, independent of row ordering."""
    rows = []
    for task in tasks:
        rows.append({name: getattr(task, name, None) for name in (
            "id", "title", "status", "tenant", "project_id", "idempotency_key", "current_run_id",
        )})
    payload = json.dumps(sorted(rows, key=lambda row: str(row["id"])), sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _clip(value: str, budget: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= budget:
        return value
    return data[:budget].decode("utf-8", errors="ignore") + "\n[truncated]"


def build_prompt(*, objective: str, repo_summary: str, board_summary: str, board_revision: str,
                 contract_summary: str, evidence_summary: str = "",
                 max_bytes: int = DEFAULT_PROMPT_BYTES) -> str:
    """Build a compact prompt with deterministic per-section byte budgets."""
    if max_bytes < 2048:
        raise PlanError("prompt budget must be at least 2048 bytes")
    fixed = f"""You are a bounded engineering planner. Return JSON only, without fences.
Do not use tools and do not mutate files, Git, or Kanban.
Select exactly one next writer task. Prefer the smallest independently verifiable fix.
The task must use schema_version=1 and fields accepted by Hermes bounded task specs.
Top-level fields must be exactly: schema_version, board_revision, task.
Echo board_revision exactly as {json.dumps(board_revision)}.
Keep title, objective, and acceptance concise. Never widen allowed_paths beyond the evidence.

OBJECTIVE\n{{objective}}
REPOSITORY\n{{repo}}
CONTRACT\n{{contract}}
KANBAN\n{{board}}
EVIDENCE\n{{evidence}}
"""
    if not isinstance(objective, str) or not objective.strip():
        raise PlanError("objective must be a non-empty string")
    remaining = max_bytes - len(fixed.format(objective="", repo="", contract="", board="", evidence="").encode())
    if remaining < 0:
        raise PlanError("prompt budget cannot contain planner contract")
    objective_budget = min(2048, remaining * 20 // 100)
    repo_budget, contract_budget = remaining * 30 // 100, remaining * 20 // 100
    board_budget = remaining * 10 // 100
    evidence_budget = remaining - objective_budget - repo_budget - contract_budget - board_budget
    prompt = fixed.format(objective=_clip(objective.strip(), objective_budget),
                          repo=_clip(repo_summary, repo_budget),
                          contract=_clip(contract_summary, contract_budget),
                          board=_clip(board_summary, board_budget),
                          evidence=_clip(evidence_summary, evidence_budget))
    while len(prompt.encode()) > max_bytes:
        prompt = prompt[:-1]
    return prompt


def invoke_planner(prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Make exactly one isolated, tool-free request through pinned Hermes internals."""
    result = run_isolated_planner(prompt)
    if len(result.text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise PlanError("planner_response_too_large")
    try:
        raw = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise PlanError("planner_non_json") from exc
    if not isinstance(raw, dict):
        raise PlanError("planner response must be an object")
    metrics = {"api_requests": result.api_requests, "input_tokens": result.input_tokens,
               "output_tokens": result.output_tokens,
               "missing_metrics_reasons": dict(result.missing_metrics_reasons)}
    return raw, metrics


def validate_plan(raw: Any, *, expected_revision: str,
                  verification_ids: Collection[str]) -> CanonicalPlan:
    """Validate a one-card plan and bind it to the observed board revision."""
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "board_revision", "task"}:
        raise PlanError("plan fields must be schema_version, board_revision, task")
    if raw["schema_version"] != 1:
        raise PlanError("unsupported plan schema_version")
    if not isinstance(raw["board_revision"], str) or not raw["board_revision"]:
        raise PlanError("board_revision must be a non-empty string")
    if raw["board_revision"] != expected_revision:
        raise PlanError("stale_board_revision")
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .spec import canonicalize_spec
        else:
            from spec import canonicalize_spec
        task = canonicalize_spec(raw["task"], verification_ids=verification_ids)
    except ValueError as exc:
        raise PlanError(f"invalid_task: {exc}") from exc
    normalized = {"schema_version": 1, "board_revision": expected_revision,
                  "task": json.loads(task.canonical_json)}
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return CanonicalPlan(canonical, hashlib.sha256(canonical).hexdigest(),
                         task.sha256, normalized["task"], expected_revision)


def store_plan(hermes_home: Path, plan: CanonicalPlan) -> Path:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .storage import store_immutable_json
    else:
        from storage import store_immutable_json
    return store_immutable_json(hermes_home, plan.canonical_json, _namespace="plans")


def preflight_apply(tasks: Collection[Any], *, expected_revision: str,
                    idempotency_key: str) -> Any | None:
    """Return an existing exact task before rejecting a stale absent plan."""
    same = next((task for task in tasks if getattr(task, "idempotency_key", None) == idempotency_key
                 and getattr(task, "status", None) != "archived"), None)
    if same is not None:
        return same
    if board_revision(tuple(t for t in tasks if getattr(t, "status", None) != "archived")) != expected_revision:
        raise PlanError("stale_board_revision")
    return None


def generate(args) -> dict[str, Any]:
    """Snapshot bounded local context, invoke one planner request, and persist its plan."""
    from hermes_cli import kanban_db as kb
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .contract import load_contract
        from .create_cli import _git
    else:
        from contract import load_contract
        from create_cli import _git
    root = Path(_git(Path(args.repo).expanduser(), "rev-parse", "--show-toplevel")).resolve()
    contract = load_contract(root)
    with kb.connect_closing(board=args.board) as conn:
        tasks = kb.list_tasks(conn, tenant="bounded-engineering/v1", include_archived=False)
    revision = board_revision(tasks)
    tracked = _git(root, "ls-files").splitlines()
    repo_summary = json.dumps({"root": str(root), "head": _git(root, "rev-parse", "HEAD"),
        "status": _git(root, "status", "--porcelain=v1", "--untracked-files=no"),
        "recent_commits": _git(root, "log", "-5", "--pretty=%h %s").splitlines(),
        "tracked_files": tracked[:400]}, separators=(",", ":"))
    board_summary = json.dumps([{"id": getattr(t, "id", None), "title": getattr(t, "title", None),
                                 "status": getattr(t, "status", None)} for t in tasks], separators=(",", ":"))
    contract_summary = json.dumps({"project_id": contract.project_id,
        "verification": [{"id": v.id, "argv": list(v.argv)} for v in contract.verification]}, separators=(",", ":"))
    evidence_summary = ""
    if getattr(args, "evidence", None):
        evidence_path = Path(args.evidence).expanduser()
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise PlanError("evidence must be a regular file")
        if evidence_path.stat().st_size > 65_536:
            raise PlanError("evidence exceeds 65536-byte cap")
        evidence_summary = evidence_path.read_text(encoding="utf-8")
    prompt = build_prompt(objective=args.objective, repo_summary=repo_summary, board_summary=board_summary,
                          board_revision=revision, contract_summary=contract_summary,
                          evidence_summary=evidence_summary,
                          max_bytes=args.max_prompt_bytes)
    if getattr(args, "response", None):
        raw = json.loads(Path(args.response).read_text(encoding="utf-8"))
        metrics = {"api_requests": 0, "input_tokens": None, "output_tokens": None,
                   "missing_metrics_reasons": {"input_tokens": "offline_replay",
                                               "output_tokens": "offline_replay"}}
    else:
        raw, metrics = invoke_planner(prompt)
    plan = validate_plan(raw, expected_revision=revision,
                         verification_ids={v.id for v in contract.verification})
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    plan_ref = store_plan(home, plan)
    output = str(plan_ref)
    if getattr(args, "output", None):
        replay = Path(args.output).expanduser()
        replay.parent.mkdir(parents=True, exist_ok=True)
        payload = plan.canonical_json + b"\n"
        if replay.exists():
            if replay.read_bytes() != payload:
                raise PlanError("refusing to overwrite different plan output")
        else:
            descriptor = os.open(replay, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        output = str(replay)
    return {"schema_version": 1, "plan_sha256": plan.sha256,
            "task_spec_sha256": plan.task_sha256, "board_revision": revision,
            "plan_ref": str(plan_ref), "output": output, "model": MODEL,
            "reasoning_effort": REASONING_EFFORT, "model_metrics": metrics,
            "prompt_bytes": len(prompt.encode())}


def apply(args) -> dict[str, Any]:
    """Validate a saved plan against the current board and use native task creation."""
    from hermes_cli import kanban_db as kb
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .contract import load_contract
        from .create_cli import create, _git
    else:
        from contract import load_contract
        from create_cli import create, _git
    root = Path(_git(Path(args.repo).expanduser(), "rev-parse", "--show-toplevel")).resolve()
    contract = load_contract(root)
    raw = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("board_revision"), str):
        raise PlanError("invalid saved plan")
    plan = validate_plan(raw, expected_revision=raw["board_revision"],
                         verification_ids={v.id for v in contract.verification})
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    directory = hermes_home / "engineering" / "apply"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{plan.task_sha256}.", suffix=".json", dir=directory)
    spec_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(plan.task, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode())
        create_args = type("CreateArgs", (), {"repo": str(root), "spec": str(spec_path),
            "board": args.board, "expected_board_revision": plan.board_revision})()
        result = create(create_args)
    finally:
        spec_path.unlink(missing_ok=True)
    result.update({"plan_sha256": plan.sha256, "board_revision": plan.board_revision})
    return result


if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .planner_runtime import run_isolated_planner
else:
    from planner_runtime import run_isolated_planner
