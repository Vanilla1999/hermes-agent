from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".product-truth" / "real_task_pack.py"

text = TARGET.read_text(encoding="utf-8")
old = '''        if any(\n            not isinstance(item.get("hidden_base"), Mapping)\n            or item["hidden_base"].get("returncode") != 1\n            for item in attempts\n        ):\n            raise ValueError(\n                "hidden-base test must fail with pytest assertion code 1"\n            )\n        expected_gold = all(item.get("passed") is True for item in attempts)\n'''
new = '''        attempt_results: list[bool] = []\n        for item in attempts:\n            if not isinstance(item, Mapping):\n                raise ValueError("attempt row must be an object")\n\n            def returncode(field: str) -> object:\n                stage = item.get(field)\n                return stage.get("returncode") if isinstance(stage, Mapping) else None\n\n            if returncode("hidden_base") != 1:\n                raise ValueError(\n                    "hidden-base test must fail with pytest assertion code 1"\n                )\n            actual_passed = bool(\n                returncode("public_base") == 0\n                and item.get("patch_applied") is True\n                and item.get("gold_surface_exact") is True\n                and returncode("public_gold") == 0\n                and returncode("hidden_gold") == 0\n            )\n            if item.get("passed") is not actual_passed:\n                raise ValueError("attempt pass claim drift")\n            attempt_results.append(actual_passed)\n\n        expected_gold = all(attempt_results)\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one attempt-verifier anchor, found {text.count(old)}")
text = text.replace(old, new, 1)

old = '''        if row.get("real_model_oracle_executed") is not False or row.get("valid") is not False:\n            raise ValueError("gold control cannot self-authorize model-oracle validity")\n'''
new = '''        if (\n            row.get("real_model_oracle_executed") is not False\n            or row.get("real_model_oracle_passed") is not False\n            or row.get("valid") is not False\n        ):\n            raise ValueError("gold control cannot self-authorize model-oracle validity")\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one oracle-claim anchor, found {text.count(old)}")
text = text.replace(old, new, 1)

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
print("real-task report verifier hardening materialized")
