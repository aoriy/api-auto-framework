"""断言小工具：返回可复用断言回调，配合 BaseCase.check(expect=...) 使用。

之所以做成「返回函数」而不是直接 assert，是为了让断言能挂在步骤结果上，
失败时能被 runner 统一收集进报告，而不是中断整个用例。
"""
from __future__ import annotations

from core.response import dig

_UNSET = object()


def status(code: int):
    def _check(response):
        assert response.status_code == code, \
            f"期望状态码 {code}，实际 {response.status_code}，响应: {response.text[:150]}"
    return _check


def ok():
    def _check(response):
        assert response.ok, f"期望 2xx，实际 {response.status_code}: {response.text[:150]}"
    return _check


def field(path: str, expected=_UNSET, *, exists: bool = True):
    """断言响应体某个路径：存在且非空 / 等于期望值 / 不存在。"""
    def _check(response):
        body = response.json(default={}) or {}
        try:
            actual = dig(body, path)
        except Exception:
            assert not exists, \
                f"期望字段 '{path}' 存在，实际响应: {str(body)[:150]}"
            return
        if not exists:
            raise AssertionError(f"期望字段 '{path}' 不存在，实际值为 {actual!r}")
        if expected is not _UNSET:
            assert actual == expected, f"字段 '{path}' 期望 {expected!r}，实际 {actual!r}"
        else:
            assert actual not in (None, "", [], {}), f"字段 '{path}' 为空值: {actual!r}"
    return _check


def faster_than(milliseconds: float):
    def _check(response):
        assert response.elapsed_ms < milliseconds, \
            f"耗时 {response.elapsed_ms}ms 超过阈值 {milliseconds}ms"
    return _check


def all_of(*checks):
    """把多个断言串成一个。"""
    def _check(response):
        for check in checks:
            check(response)
    return _check
