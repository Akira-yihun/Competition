"""Dependency-free test runner.

The submission sandbox is offline and the base interpreter has no test
framework, so the suite must run on a bare ``python3``.  Test classes/functions
keep the usual ``test_*`` naming, so ``pytest`` can still drive the same files
where it is available.
"""
from __future__ import annotations

import importlib
import inspect
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))


def run_module(module) -> int:
    passed, failed = 0, []
    for name, obj in sorted(vars(module).items()):
        if name.startswith("test_") and inspect.isfunction(obj):
            try:
                obj()
                passed += 1
            except Exception:
                failed.append((f"{module.__name__}::{name}", traceback.format_exc()))
        elif name.startswith("Test") and inspect.isclass(obj):
            for method_name, method in sorted(vars(obj).items()):
                if not method_name.startswith("test_"):
                    continue
                label = f"{module.__name__}::{name}::{method_name}"
                try:
                    instance = obj()
                    setup = getattr(instance, "setup_method", None)
                    if callable(setup):
                        setup()
                    getattr(instance, method_name)()
                    passed += 1
                except Exception:
                    failed.append((label, traceback.format_exc()))
    for label, tb in failed:
        print(f"FAIL {label}\n{tb}")
    print(f"{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


def main() -> int:
    modules = sys.argv[1:] or ["test_contract", "test_agent"]
    status = 0
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            print(f"SKIP {module_name}: {exc}")
            continue
        print(f"== {module_name}")
        status |= run_module(module)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
