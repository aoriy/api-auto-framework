"""鉴权策略基类。

一个策略 = 「凭证放在请求的哪里」+「凭证从登录响应/会话哪里取」+「会话怎么维护」。

子类需要回答三件事：
  1. requires_credential —— 这次访问要不要先登录拿到凭证（none / basic 不要）
  2. uses_session       —— 凭证是否绑定在 CookieJar 上（cookie 要）
  3. apply(...)         —— 把凭证写到 headers / params / cookies 的哪里

取凭证、算过期、抓会话快照由基类统一处理，子类不用管。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.exceptions import AuthError
from core.response import ApiResponse, dig


class AuthStrategy(ABC):
    type: str = "base"

    #: 凭证是否绑定在 CookieJar 上。
    #: True 意味着「登录必须发生在这个账号自己的 requests.Session 上」——
    #: 否则 Set-Cookie 会落进另一个账号的 CookieJar，导致串号。
    uses_session: bool = False

    def __init__(self, spec: dict | None = None):
        self.spec = spec or {}
        self.token_path: str = self.spec.get("token_path") or "data.access_token"
        self.expires_path: str | None = self.spec.get("expires_path")
        self.refresh_path: str | None = self.spec.get("refresh_path")

    # ------------------------------------------------------------------
    # 子类契约
    # ------------------------------------------------------------------
    @property
    def requires_credential(self) -> bool:
        """是否需要先登录才能访问。决定收到 401 时要不要自动重登。"""
        return True

    @property
    def needs_token(self) -> bool:
        """是否需要把凭证作为 token 值显式注入请求。

        Cookie 由 Session 自动携带，因此 cookie 类策略返回 False。
        """
        return self.requires_credential

    @abstractmethod
    def apply(self, *, headers: dict, params: dict, cookies: dict,
              token: str | None, method: str, url: str) -> None:
        """把鉴权信息写进传入的容器（原地修改）。"""

    # ------------------------------------------------------------------
    # 会话钩子：默认空实现，Cookie 类策略覆写
    # ------------------------------------------------------------------
    def prepare(self, *, session, headers: dict, method: str, url: str) -> None:
        """发送前调用。可以从 session（CookieJar）读信息补进请求头。

        典型用途：把 CookieJar 里的 csrftoken 复制到 X-CSRFToken 头。
        """

    def snapshot(self, session) -> dict:
        """登录成功后抓一份会话快照，用于落盘 / 跨次运行复用。"""
        return {}

    def restore(self, session, cookies: dict) -> None:
        """把一个会话快照灌回 session。"""

    def clear_session(self, session) -> None:
        """作废该账号的会话。重登前调用，避免旧 Cookie 干扰。"""
        if session is not None:
            session.cookies.clear()

    # ------------------------------------------------------------------
    # 基类公共能力
    # ------------------------------------------------------------------
    def extract(self, login_response: ApiResponse) -> str:
        """从登录响应中取出凭证值（token 类策略用）。"""
        if login_response is None:
            raise AuthError("登录没有返回响应，无法提取凭证")
        try:
            token = dig(login_response.json() or {}, self.token_path)
        except Exception as exc:
            body = str(login_response.json())[:200]
            raise AuthError(
                f"登录响应中找不到凭证路径 '{self.token_path}'，实际响应: {body}"
            ) from exc
        if not token:
            raise AuthError(f"凭证路径 '{self.token_path}' 取到空值")
        return str(token)

    def extract_expires(self, login_response: ApiResponse, default: int = 3600) -> int:
        """取凭证有效期（秒）；拿不到就按默认值处理。"""
        if not self.expires_path:
            return default
        try:
            return int(dig(login_response.json() or {}, self.expires_path))
        except Exception:
            return default

    def describe(self) -> str:
        """给 --list-accounts 用的一句话说明。"""
        return self.type

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"
