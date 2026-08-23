"""Deterministic top-level CLI for bounded engineering."""
from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import yaml


GATE_NAME = "bounded-engineering/v1"
_ENGINEERING_TOML = """schema_version = 1
completion_gate = \"bounded-engineering/v1\"

# Declare bounded verification commands explicitly; this command never guesses them.
verification = []
"""
_ENGINEERING_MD = """# Bounded Engineering Policy

This repository is managed only through explicit, immutable engineering contracts.
Declare allowed paths and verification commands in `engineering.toml` before creating work.
No profile, board, model, or provider setting is inferred by this file.
"""


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="engineering_action", required=True)
    doctor = sub.add_parser("doctor", help="Check bounded-engineering local prerequisites")
    doctor.add_argument("--repo", required=True, help="Repository path")
    doctor.add_argument("--board", required=True, help="Dedicated Kanban board")
    doctor.add_argument("--profile", default="bounded-engineer", help="Dedicated profile name")
    doctor.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .operator_cli import setup_operator_parsers
    else:  # Direct-module tests and scripts.
        from operator_cli import setup_operator_parsers
    setup_operator_parsers(sub)
    init_repo = sub.add_parser("init-repo", help="Render bounded repository policy templates")
    init_repo.add_argument("--repo", required=True, help="Repository path")
    init_repo.add_argument("--write", action="store_true", help="Create only missing policy files")
    init_repo.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    validate = sub.add_parser("validate", help="Validate an explicit bounded task specification")
    validate.add_argument("--repo", required=True, help="Repository path")
    validate.add_argument("--spec", required=True, help="JSON task specification path")
    validate.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    create = sub.add_parser("create", help="Create one deterministic bounded engineering task")
    create.add_argument("--repo", required=True, help="Repository Git top-level path")
    create.add_argument("--spec", required=True, help="Canonical JSON task specification path")
    create.add_argument("--board", required=True, help="Dedicated Kanban board")
    create.add_argument("--expected-baseline-head", help="Operator-pinned repository HEAD")
    create.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    plan = sub.add_parser("plan", help="Generate one token-bounded next-task plan")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--board", required=True)
    plan.add_argument("--objective", required=True, help="Explicit goal for selecting the next card")
    plan.add_argument("--evidence", help="Optional bounded UTF-8 issue/test evidence")
    plan.add_argument("--output", help="Optional immutable replay copy; authoritative plan_ref is always stored")
    plan.add_argument("--response", help="Use a captured planner JSON response (testing/offline)")
    plan.add_argument("--max-prompt-bytes", type=int, default=12288)
    plan.add_argument("--json", action="store_true", dest="json_output")
    apply_plan = sub.add_parser("apply-plan", help="CAS-apply one validated plan to native Kanban")
    apply_plan.add_argument("--repo", required=True)
    apply_plan.add_argument("--board", required=True)
    apply_plan.add_argument("--plan", required=True)
    apply_plan.add_argument("--json", action="store_true", dest="json_output")


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _repo_root(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _profile_policy_checks(profile_dir: Path) -> list[dict[str, object]]:
    config_path = profile_dir / "config.yaml"
    if not config_path.is_file():
        return [_check("profile_policy", False, "profile config.yaml does not exist")]
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [_check("profile_policy", False, f"invalid profile config: {exc}")]
    if not isinstance(config, dict):
        return [_check("profile_policy", False, "profile config must be a mapping")]
    terminal = config.get("terminal")
    if not isinstance(terminal, dict):
        return [_check("profile_policy", False, "terminal config must be a mapping")]
    volumes = terminal.get("docker_volumes", [])
    docker_env = terminal.get("docker_env", {})
    restricted_proxy_env = {
        "HTTP_PROXY": "http://hermes-egress-proxy:3128",
        "HTTPS_PROXY": "http://hermes-egress-proxy:3128",
        "DOCMANCER_WEB_FETCH_USE_ENV_PROXY": "true",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/workspace",
        "PYTHONPATH": "/workspace",
        "http_proxy": "http://hermes-egress-proxy:3128",
        "https_proxy": "http://hermes-egress-proxy:3128",
    }
    approved_docmancer_storage = (
        isinstance(volumes, list)
        and len(volumes) == 2
        and docker_env in (
            {},
            {"DOCMANCER_HOME": "/runtime"},
            restricted_proxy_env,
            {**restricted_proxy_env, "DOCMANCER_HOME": "/runtime"},
        )
        and {
            tuple(str(volume).rsplit(":", 2)[1:])
            for volume in volumes
            if len(str(volume).rsplit(":", 2)) == 3
            and Path(str(volume).rsplit(":", 2)[0]).is_relative_to("/srv/hermes-lab/operator")
        } == {("/index", "ro"), ("/runtime", "rw")}
    )
    isolated = (
        terminal.get("backend") == "docker"
        and terminal.get("docker_forward_env") == []
        and terminal.get("docker_mount_cwd_to_workspace") is True
        and terminal.get("docker_run_as_host_user") is False
        and terminal.get("docker_network") is True
        and terminal.get("docker_extra_args", []) == [
            "--network=hermes-restricted",
            "--dns=172.18.0.2",
        ]
        and docker_env in (
            restricted_proxy_env,
            {**restricted_proxy_env, "DOCMANCER_HOME": "/runtime"},
        )
        and ((volumes == [] and docker_env == {}) or approved_docmancer_storage)
        and terminal.get("docker_persist_across_processes") is False
        and terminal.get("docker_mount_host_data") is False
    )
    plugins_value = config.get("plugins")
    plugins: dict[str, object] = plugins_value if isinstance(plugins_value, dict) else {}
    kanban_value = config.get("kanban")
    kanban: dict[str, object] = kanban_value if isinstance(kanban_value, dict) else {}
    image = terminal.get("docker_image")
    enabled_value = plugins.get("enabled")
    plugin_enabled = isinstance(enabled_value, list) and "bounded-engineering" in enabled_value
    return [
        _check("profile_policy", isolated, "docker isolation settings" if isolated else "required Docker isolation settings are missing"),
        _check(
            "network_isolation",
            terminal.get("docker_network") is True
            and terminal.get("docker_extra_args", []) == [
                "--network=hermes-restricted",
                "--dns=172.18.0.2",
            ]
            and docker_env in (
                restricted_proxy_env,
                {**restricted_proxy_env, "DOCMANCER_HOME": "/runtime"},
            ),
            "restricted proxy-only Docker network",
        ),
        _check("plugin_enabled", plugin_enabled, "bounded-engineering explicitly enabled"),
        _check("auto_decompose", kanban.get("auto_decompose") is False, "kanban.auto_decompose is false"),
        _check("writer_concurrency", kanban.get("max_in_progress") == 1 and kanban.get("max_in_progress_per_profile") == 1, "global and per-profile maximum are one"),
        _check("image_pinned", isinstance(image, str) and image.startswith("sha256:") and len(image) == 71, str(image)),
    ]


def _container_probe_checks(profile_dir: Path, repo_root: str) -> list[dict[str, object]]:
    try:
        config = yaml.safe_load(
            (profile_dir / "config.yaml").read_text(encoding="utf-8")
        )
        image = config["terminal"]["docker_image"]
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
        if inspect.returncode != 0:
            return [_check("docker_image_local", False, "configured image is not available locally")]
        probe = subprocess.run([
            "docker", "run", "--rm", "--pull=never", "--network=none",
            "-v", f"{repo_root}:/workspace:ro", "-w", "/workspace", image,
            "sh", "-c",
            "test \"$(pwd)\" = /workspace && test ! -e /var/run/docker.sock && "
            "test ! -e /home/viadmin/.hermes/kanban.db && test -z \"${SSH_AUTH_SOCK:-}\"",
        ],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=45,
        )
    except (KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        return [_check("container_probe", False, f"container probe failed: {exc}")]
    return [
        _check("docker_image_local", True, image),
        _check("container_probe", probe.returncode == 0, "network-none read-only workspace probe"),
    ]


def _repository_policy_checks(repo_root: str) -> list[dict[str, object]]:
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .contract import load_contract
        else:  # Direct-module tests and scripts.
            from contract import load_contract
        contract = load_contract(Path(repo_root))
    except (ImportError, ValueError) as exc:
        return [_check("repository_contract", False, str(exc))]
    head = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    checks = [_check("repository_contract", True, contract.sha256), _check("baseline_head", head.returncode == 0, head.stdout.strip() or "HEAD is unreadable")]
    if contract.workspace.get("require_clean_source") is True:
        status = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        checks.append(_check("clean_source", status.returncode == 0 and not status.stdout, "clean" if not status.stdout else "source repository is dirty"))
    return checks


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def _run_doctor(args) -> int:
    repo = Path(args.repo).expanduser()
    checks: list[dict[str, object]] = []
    root = _repo_root(repo) if repo.is_dir() else None
    checks.append(_check("repository", root is not None, root or "not a readable Git repository"))
    if root is not None:
        checks.extend(_repository_policy_checks(root))
    from hermes_cli import kanban_db as kb
    board_ok = kb.board_exists(args.board)
    checks.append(_check("board", board_ok, args.board if board_ok else "board does not exist"))
    from hermes_cli.profiles import get_profile_dir
    profile_dir = get_profile_dir(args.profile)
    profile_exists = profile_dir.is_dir()
    checks.append(_check("profile", profile_exists, str(profile_dir) if profile_exists else "profile does not exist"))
    if profile_exists:
        checks.extend(_profile_policy_checks(profile_dir))
        if root is not None:
            checks.extend(_container_probe_checks(profile_dir, root))
    manifest = Path(__file__).with_name("plugin.yaml")
    checks.append(_check("plugin_manifest", manifest.is_file(), str(manifest)))
    gate_api = getattr(kb, "complete_task_with_gate", None)
    gate_ok = callable(gate_api) and {"task_id", "gate_name", "expected_run_id"}.issubset(inspect.signature(gate_api).parameters)
    checks.append(_check("completion_gate", gate_ok, GATE_NAME))
    payload = {"ready": all(bool(item["ok"]) for item in checks), "checks": checks, "schema_version": 1}
    _emit(payload, json_output=args.json_output)
    return 0 if payload["ready"] else 1


def _run_operator(args) -> int:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .operator_cli import run_operator
    else:  # Direct-module tests and scripts.
        from operator_cli import run_operator
    code, payload = run_operator(args)
    _emit(payload, json_output=args.json_output)
    return code


def _run_init_repo(args) -> int:
    repo = Path(args.repo).expanduser()
    root = _repo_root(repo) if repo.is_dir() else None
    if root is None:
        _emit({"error": "invalid_repository", "schema_version": 1}, json_output=args.json_output)
        return 1
    policy_dir = Path(root) / ".hermes"
    files = {policy_dir / "engineering.toml": _ENGINEERING_TOML, policy_dir / "ENGINEERING.md": _ENGINEERING_MD}
    existing = [str(path) for path in files if path.exists()]
    payload = {"schema_version": 1, "repository": root, "files": [str(path) for path in files], "existing": existing, "written": False}
    if existing or not args.write:
        _emit(payload, json_output=args.json_output)
        return 1
    policy_dir.mkdir(parents=True)
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    payload["written"] = True
    _emit(payload, json_output=args.json_output)
    return 0


def _run_validate(args) -> int:
    repo = Path(args.repo).expanduser()
    root = _repo_root(repo) if repo.is_dir() else None
    if root is None:
        _emit({"error": "invalid_repository", "schema_version": 1}, json_output=args.json_output)
        return 1
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .contract import load_contract
            from .spec import canonicalize_spec
        else:  # Direct-module tests and scripts.
            from contract import load_contract
            from spec import canonicalize_spec
        contract = load_contract(Path(root))
        raw_spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        spec = canonicalize_spec(raw_spec, verification_ids={item.id for item in contract.verification})
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _emit({"error": "validation_failed", "detail": str(exc), "schema_version": 1}, json_output=args.json_output)
        return 1
    _emit({"schema_version": 1, "contract_sha256": contract.sha256, "spec_sha256": spec.sha256,
           "allowed_roots": list(spec.allowed_roots), "allowed_files": list(spec.allowed_files),
           "risk": raw_spec["risk"], "verification_ids": list(raw_spec["required_verification_ids"])}, json_output=args.json_output)
    return 0


def _run_create(args) -> int:
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .create_cli import create
        else:  # Direct-module tests and scripts.
            from create_cli import create
        payload = create(args)
    except (RuntimeError, ValueError) as exc:
        _emit({"error": "creation_failed", "detail": str(exc), "schema_version": 1},
              json_output=args.json_output)
        return 1
    _emit(payload, json_output=args.json_output)
    return 0


def _run_plan(args, *, apply_plan: bool) -> int:
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .plan_cli import apply, generate
        else:
            from plan_cli import apply, generate
        payload = apply(args) if apply_plan else generate(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        _emit({"error": "plan_failed", "detail": str(exc), "schema_version": 1},
              json_output=args.json_output)
        return 1
    _emit(payload, json_output=args.json_output)
    return 0


def run(args) -> int:
    if args.engineering_action == "doctor":
        return _run_doctor(args)
    if args.engineering_action in {"status", "proof", "report", "prepare-task-push", "override-block", "pilot"}:
        return _run_operator(args)
    if args.engineering_action == "init-repo":
        return _run_init_repo(args)
    if args.engineering_action == "validate":
        return _run_validate(args)
    if args.engineering_action == "create":
        return _run_create(args)
    if args.engineering_action == "plan":
        return _run_plan(args, apply_plan=False)
    if args.engineering_action == "apply-plan":
        return _run_plan(args, apply_plan=True)
    raise ValueError(f"unsupported engineering action: {args.engineering_action}")


def entrypoint(args) -> None:
    """Bridge plugin CLI dispatch, whose generic main loop ignores returns."""
    raise SystemExit(run(args))
