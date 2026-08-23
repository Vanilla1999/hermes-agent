from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".product-truth" / "real_task_pack.py"

text = TARGET.read_text(encoding="utf-8")
old = '                and hidden_base["returncode"] in {1, 2}\n'
new = '                and hidden_base["returncode"] == 1\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one hidden-base acceptance anchor, found {text.count(old)}")
text = text.replace(old, new, 1)

anchor = '        expected_gold = all(item.get("passed") is True for item in attempts)\n'
replacement = '''        if any(\n            not isinstance(item.get("hidden_base"), Mapping)\n            or item["hidden_base"].get("returncode") != 1\n            for item in attempts\n        ):\n            raise ValueError(\n                "hidden-base test must fail with pytest assertion code 1"\n            )\n        expected_gold = all(item.get("passed") is True for item in attempts)\n'''
if text.count(anchor) != 1:
    raise SystemExit(f"expected one report-verifier anchor, found {text.count(anchor)}")
text = text.replace(anchor, replacement, 1)

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
print("strict hidden-base assertion contract materialized")
