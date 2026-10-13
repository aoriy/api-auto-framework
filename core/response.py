"""响应封装与路径取值。

统一把 requests.Response 转成 ApiResponse，页面类和用例只跟 ApiResponse 打交道，
这样以后换 http 库（httpx/aiohttp）不用动用例代码。
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.exceptions import PathNotFound

_MISSING = object()


def dig(data: Any, path: str | None, default: Any = _MISSING) -> Any:
    """按点号路径取嵌套值，支持列表下标。

    dig(body, "data.items.0.id") -> 第一件商品的 id
    """
    if path in (None, "", "$"):
        return data
    current = data
    for part in str(path).lstrip("$").lstrip(".").split("."):
        if isinstance(current, Mapping):
            if part not in current:
                if default is _MISSING:
                    raise PathNotFound(f"路径 '{path}' 在 '{part}' 处中断")
                return default
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                if default is _MISSING:
                    raise PathNotFound(f"路径 '{path}' 的下标 '{part}' 越界或非法")
                return default
        else:
            if default is _MISSING:
                raise PathNotFound(f"路径 '{path}' 在 '{part}' 处无法继续下钻")
            return default
    return current


@dataclass
class ApiResponse:
    """一次 HTTP 调用的结果快照。"""

    method: str
    url: str
    status_code: int
    headers: dict = field(default_factory=dict)
    text: str = ""
    elapsed_ms: float = 0.0
    #: 发起这次请求的**账号名**。每个响应都带这个字段，
    #: 断言时顺手校验它，多账号串号就藏不住。
    account: str = "anonymous"
    request_kwargs: dict = field(default_factory=dict)
    _json: Any = None
    _parsed: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def role(self) -> str:
        """兼容旧字段名：现在承载的是账号名。"""
        return self.account

    def json(self, default: Any = None) -> Any:
        """JSON 自动解析；非 JSON 响应返回 default。"""
        if not self._parsed:
            try:
                self._json = _json.loads(self.text) if self.text else None
            except ValueError:
                self._json = None
            self._parsed = True
        return default if self._json is None else self._json

    def get(self, path: str, default: Any = None) -> Any:
        """从响应体按点号路径取值，取不到返回 default。"""
        return dig(self.json(default={}) or {}, path, default)

    def require(self, path: str) -> Any:
        """从响应体按点号路径取值，取不到直接抛 PathNotFound。"""
        return dig(self.json(default={}) or {}, path)

    def __str__(self) -> str:
        return f"<{self.method} {self.url} -> {self.status_code} {self.elapsed_ms}ms role={self.role}>"

    @property
    def summary(self) -> str:
        """给报告用的一行摘要。"""
        return f"{self.method} {self.url} -> {self.status_code} ({self.elapsed_ms}ms, role={self.role})"
