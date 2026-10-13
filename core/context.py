"""用例上下文：页面对象与用例之间的共享总线。

职责有三个：
  1. 提供账号客户端入口 —— ctx.client("buyer_a")
  2. 提供会话/凭证的只读视图 —— ctx.session("buyer_a")，用于断言账号隔离
  3. 提供跨接口/跨步骤的变量传递 —— ctx.set / ctx.interpolate("${order_id}")

注意：这里**不保存任何账号状态**。账号凭证全部托管在 SessionFactory，
Context 只是一个传参通道，因此不会因为切账号而出现全局状态污染。
"""
from __future__ import annotations

import re
from typing import Any

from core.exceptions import FrameworkError
from core.response import dig

_REF = re.compile(r"\$\{([^}]+)\}")


class Context:
    def __init__(self, factory, env: str):
        self.factory = factory
        self.env = env
        self._vars: dict[str, Any] = {}

    # ---------------- 账号入口 ----------------
    def client(self, account: str):
        """取某账号的客户端。该客户端已经会自己带对应凭证。"""
        return self.factory.client(account)

    def session(self, account: str):
        """取某账号的 requests.Session（只读）。用于断言「账号之间会话不共享」。"""
        return self.factory.session_of(account)

    def cookies(self, account: str) -> dict:
        """取某账号当前 CookieJar 的快照（只读）。"""
        return self.factory.cookies_of(account)

    def login(self, account: str, *, force: bool = False):
        """确保该账号凭证就绪：命中有缓存的凭证就复用，没有才真正登录。

        force=True 时才忽略缓存强制重新登录（命令行 --fresh-token 走这条路）。
        无凭证账号（guest）返回 None。
        """
        return self.factory.login(account, force=force)

    # ---------------- 环境配置（只读） ----------------
    @property
    def api_prefix(self) -> str:
        """环境配置里的默认 URL 前缀。

        页面类没有声明 `prefix` 时，BaseApi 会取它。
        见 config/env/<env>.yaml 的 api_prefix。
        """
        return getattr(self.factory.settings, "api_prefix", "") or ""

    # ---------------- 账号列表（只读） ----------------
    @property
    def available_roles(self) -> list[str]:
        return sorted(self.factory.accounts)

    #: 语义更准确的别名
    @property
    def available_accounts(self) -> list[str]:
        return sorted(self.factory.accounts)

    # ---------------- 变量 ----------------
    def set(self, key: str, value: Any) -> None:
        self._vars[key] = value

    def set_many(self, mapping: dict) -> None:
        self._vars.update(mapping)

    def get(self, key: str, default: Any = None) -> Any:
        return self._vars.get(key, default)

    @property
    def variables(self) -> dict:
        return dict(self._vars)

    # ---------------- 引用解析 ----------------
    def interpolate(self, value: Any) -> Any:
        """把 ${var} / ${var.a.b} 换成上下文里的真实值，支持 dict / list 递归。"""
        if isinstance(value, dict):
            return {k: self.interpolate(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.interpolate(v) for v in value]
        if isinstance(value, str) and "${" in value:
            return self._substitute(value)
        return value

    def _substitute(self, text: str) -> str:
        def replace(match: re.Match) -> str:
            expr = match.group(1).strip()
            if expr in self._vars:
                return str(self._vars[expr])
            head, _, rest = expr.partition(".")
            if head in self._vars:
                return str(dig(self._vars[head], rest, ""))
            raise FrameworkError(
                f"上下文里没有变量 '{expr}'，已有变量: {sorted(self._vars)}"
            )
        return _REF.sub(replace, text)
