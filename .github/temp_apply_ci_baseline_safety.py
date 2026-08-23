from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement anchor, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '''        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False, capture_output=True, text=True, timeout=10,
        )
''',
    '''        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        config = yaml.safe_load(config_path.read_text())\n',
    '        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))\n',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        config = yaml.safe_load((profile_dir / "config.yaml").read_text())\n',
    '        config = yaml.safe_load(\n            (profile_dir / "config.yaml").read_text(encoding="utf-8")\n        )\n',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        inspect = subprocess.run(["docker", "image", "inspect", image], check=False, capture_output=True, timeout=20)\n',
    '''        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '''        ], check=False, capture_output=True, timeout=45)
''',
    '''        ],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=45,
        )
''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '    head = subprocess.run(["git", "-C", repo_root, "rev-parse", "HEAD"], check=False, capture_output=True, text=True, timeout=10)\n',
    '''    head = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        status = subprocess.run(["git", "-C", repo_root, "status", "--porcelain=v1", "--untracked-files=all"], check=False, capture_output=True, text=True, timeout=10)\n',
    '''        status = subprocess.run(
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
''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        path.write_text(content)\n',
    '        path.write_text(content, encoding="utf-8")\n',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '        raw_spec = json.loads(Path(args.spec).read_text())\n',
    '        raw_spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))\n',
)

replace_once(
    "plugins/bounded-engineering/create_cli.py",
    '''        result = subprocess.run(["git", "-C", str(repo), *args], check=False,
                                capture_output=True, text=True, timeout=10)
''',
    '''        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
''',
)
replace_once(
    "plugins/bounded-engineering/create_cli.py",
    '''    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                             f"refs/heads/{branch}"], check=False, capture_output=True,
                            text=True, timeout=10)
''',
    '''    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
''',
)

replace_once(
    "plugins/bounded-engineering/plan_cli.py",
    '        raw = json.loads(Path(args.response).read_text())\n',
    '        raw = json.loads(Path(args.response).read_text(encoding="utf-8"))\n',
)
replace_once(
    "plugins/bounded-engineering/plan_cli.py",
    '    raw = json.loads(Path(args.plan).expanduser().read_text())\n',
    '    raw = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))\n',
)

replace_once(
    "tools/terminal_tool.py",
    '''    from hermes_cli.config import apply_terminal_config_to_env
    apply_terminal_config_to_env()

    # Default image with Python and Node.js for maximum compatibility
    default_image = "nikolaik/python-nodejs:python3.11-nodejs20"
    _ensure_terminal_env_bridged()
''',
    '''    # Bridge once and fail open to the historical environment defaults.
    # The bridge owns exception containment and explicit-config precedence.
    _ensure_terminal_env_bridged()

    # Default image with Python and Node.js for maximum compatibility
    default_image = "nikolaik/python-nodejs:python3.11-nodejs20"
''',
)

replace_once(
    "tests/tools/test_terminal_env_bridge.py",
    '    (home / "config.yaml").write_text(text)\n',
    '    (home / "config.yaml").write_text(text, encoding="utf-8")\n',
)

for relative in (
    "plugins/bounded-engineering/engineering_cli.py",
    "plugins/bounded-engineering/create_cli.py",
    "plugins/bounded-engineering/plan_cli.py",
    "tools/terminal_tool.py",
    "tests/tools/test_terminal_env_bridge.py",
):
    compile((ROOT / relative).read_text(encoding="utf-8"), relative, "exec")

print("CI baseline safety repair materialized")
