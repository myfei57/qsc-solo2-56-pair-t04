#!/usr/bin/env python3
"""用 pytest 垫片运行 tests/ 下全部用例（零第三方依赖）。

用法：python3 tools/run_tests.py
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import pytest_shim  # noqa: E402

sys.modules["pytest"] = pytest_shim

from psdline.app import build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402

TESTS = ROOT / "tests"


def _load_module(path: pathlib.Path):
    name = f"testmod_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _conftest_fixtures():
    """从 conftest.py 提取 fixture 工厂。"""

    conftest = _load_module(TESTS / "conftest.py")
    fixtures = {}
    for name, value in vars(conftest).items():
        if isinstance(value, pytest_shim.FixtureWrapper):
            fixtures[name] = value.func
    return fixtures


def main() -> int:
    fixtures = _conftest_fixtures()
    passed = failed = 0
    failures: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        module = _load_module(path)
        for name, value in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(value):
                continue
            kwargs = {}
            params = inspect.signature(value).parameters
            tmpdir = None
            need_platform = "platform" in params or "online" in params
            need_clock = "clock" in params
            clock = ManualClock() if (need_clock or need_platform) else None
            platform = None
            if need_platform:
                tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="psdline-test-"))
                platform = build_platform(
                    default_config(), clock=clock, storage_root=str(tmpdir / "state")
                )
            for param in params:
                if param == "tmp_path":
                    kwargs["tmp_path"] = pathlib.Path(tempfile.mkdtemp(prefix="psdline-test-"))
                elif param == "clock":
                    kwargs["clock"] = clock
                elif param == "platform":
                    kwargs["platform"] = platform
                elif param == "online":
                    platform.start()
                    platform.arrive("T01", "ST1", 0)
                    platform.check_gap("T01")
                    kwargs["online"] = platform
            try:
                value(**kwargs)
            except Exception:
                failed += 1
                failures.append(f"{path.name}::{name}\n{traceback.format_exc()}")
            else:
                passed += 1
    print(f"{passed} passed, {failed} failed")
    for item in failures:
        print("=" * 70)
        print(item)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
