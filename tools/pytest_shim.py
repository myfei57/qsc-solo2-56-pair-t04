"""零依赖 pytest 垫片（仅覆盖本仓库实际使用的 API）。

运行环境可能装不了 pytest，而仓库里的测试只用到 ``pytest.fixture`` 与
``pytest.raises``。这个垫片提供同名能力，由 ``tools/run_tests.py`` 注入
``sys.modules`` 后以普通 unittest 风格收集并执行 ``tests/`` 下的全部用例。
"""

from __future__ import annotations

import contextlib
import inspect


class FixtureWrapper:
    """``@pytest.fixture()`` 标记过的工厂函数。"""

    def __init__(self, func):
        self.func = func
        self.__wrapped__ = func


def fixture(*_args, **_kwargs):
    def wrap(func):
        return FixtureWrapper(func)

    if _args and callable(_args[0]):
        return FixtureWrapper(_args[0])
    return wrap


@contextlib.contextmanager
def raises(expected):
    caught = None
    try:
        yield
    except expected as error:
        caught = error
    except Exception as error:  # 类型不符也要报
        raise AssertionError(f"期望抛出 {expected!r}，实际抛出 {type(error).__name__}: {error}")
    if caught is None:
        raise AssertionError(f"期望抛出 {expected!r}，但没有异常")


class _Mark:
    def __getattr__(self, _name):
        def noop(*_args, **_kwargs):
            return None

        return noop


mark = _Mark()
skip = _Mark()
