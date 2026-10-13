"""内置鉴权策略族。

accounts.yaml 里 auth.type 写哪个名字，就用哪个类：

  cookie     会话 Cookie（**本项目主用**）：登录后凭证落在该账号自己的
             requests.Session 里，由 Session 自动携带；写操作自动回填 CSRF 头
  bearer     Authorization: Bearer <token>（Authorization 可选入口）
  header     自定义请求头，如 X-Merchant-Token: <token>
  query      URL 参数，如 ?access_token=<token>
  basic      HTTP Basic，直接用凭据，不需要登录
  signature  HMAC 签名（时间戳参与），token 当密钥
  none       公开接口，不带任何凭证
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from core.auth.base import AuthStrategy
from core.auth.registry import register
from core.exceptions import ConfigError

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@register
class NoAuth(AuthStrategy):
    """无鉴权。"""

    type = "none"

    @property
    def requires_credential(self) -> bool:
        return False

    @property
    def needs_token(self) -> bool:
        return False

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        return

    def describe(self) -> str:
        return "无鉴权（公开接口）"


@register
class CookieAuth(AuthStrategy):
    """会话 Cookie 鉴权（本项目主用）。

    与 token 类策略的根本区别：**凭证不是"一个值"，而是"一个 CookieJar"**。
    因此这个策略不负责把值写进请求，而是负责三件事：

      1. uses_session = True
         告诉框架「登录必须发生在该账号自己的 requests.Session 上」，
         否则服务端下发的 Set-Cookie 会落进别的账号的 CookieJar → 串号。

      2. prepare()
         写操作前从本账号的 CookieJar 里取 csrftoken，复制到 X-CSRFToken 头。
         CSRF token 与 session 绑定，换账号后必须跟着换，否则 403。

      3. snapshot() / restore() / clear_session()
         会话快照的抓取、恢复与作废，供落盘复用与 401 重登使用。

    配置项（accounts.yaml 的 auth 段）：
      session_cookie  会话 Cookie 名，默认 SESSION（仅用于描述/校验）
      csrf_cookie     CSRF Cookie 名，如 csrftoken；不配则不做 CSRF 回填
      csrf_header     CSRF 头名，默认 X-CSRFToken
      assume_ttl      本地假定的会话有效期（秒），默认 0（视为不过期，靠 401 兜底）
    """

    type = "cookie"
    uses_session = True

    @property
    def needs_token(self) -> bool:
        return False

    # ---------------- 请求注入 ----------------
    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        # 会话 Cookie 由 requests.Session 自动携带，这里不需要写任何东西。
        # 保留空实现是为了让「鉴权注入」这条链路对所有策略形态一致：
        # 写法上仍然是 strategy.apply(...)，只是 cookie 类策略无值可写。
        return

    def prepare(self, *, session, headers, method, url) -> None:
        """写操作前把该账号的 CSRF token 回填到请求头。"""
        csrf_cookie = self.spec.get("csrf_cookie")
        if not csrf_cookie or session is None:
            return
        if method.upper() in _SAFE_METHODS:
            return
        header_name = self.spec.get("csrf_header", "X-CSRFToken")
        value = session.cookies.get(csrf_cookie)
        if value:
            headers[header_name] = value

    # ---------------- 会话快照 ----------------
    def snapshot(self, session) -> dict:
        if session is None:
            return {}
        return {c.name: c.value for c in session.cookies}

    def restore(self, session, cookies: dict) -> None:
        if session is None or not cookies:
            return
        for name, value in cookies.items():
            session.cookies.set(name, value)

    def extract_expires(self, login_response, default: int = 0) -> int:
        """Cookie 的过期由服务端 Set-Cookie 决定，客户端不解析。

        默认返回 0（视为不过期），完全靠 401 兜底重登；
        想减少一次无谓的 401，可以在配置里给一个 assume_ttl。
        """
        return int(self.spec.get("assume_ttl", 0))

    def describe(self) -> str:
        csrf = self.spec.get("csrf_cookie")
        extra = f" + CSRF({csrf})" if csrf else ""
        return f"会话 Cookie {self.spec.get('session_cookie', 'SESSION')}{extra}"


@register
class BearerAuth(AuthStrategy):
    """Authorization: Bearer <token>"""

    type = "bearer"

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        header = self.spec.get("header", "Authorization")
        prefix = self.spec.get("prefix", "Bearer ")
        headers[header] = f"{prefix}{token}"

    def describe(self) -> str:
        return f"{self.spec.get('header', 'Authorization')}: {self.spec.get('prefix', 'Bearer ')}<token>"


@register
class HeaderAuth(AuthStrategy):
    """自定义请求头鉴权。"""

    type = "header"

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        header = self.spec.get("header")
        if not header:
            raise ConfigError("auth.type=header 时必须指定 header 名称")
        prefix = self.spec.get("prefix", "")
        headers[header] = f"{prefix}{token}"

    def describe(self) -> str:
        return f"{self.spec.get('header')}: <token>"


@register
class QueryAuth(AuthStrategy):
    """URL 参数鉴权。"""

    type = "query"

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        params[self.spec.get("key", "access_token")] = token

    def describe(self) -> str:
        return f"?{self.spec.get('key', 'access_token')}=<token>"


@register
class BasicAuth(AuthStrategy):
    """HTTP Basic：直接吃凭据，不需要登录拿 token。"""

    type = "basic"

    @property
    def requires_credential(self) -> bool:
        return False

    @property
    def needs_token(self) -> bool:
        return False

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        user = self.spec.get("username")
        password = self.spec.get("password")
        if not user or password is None:
            raise ConfigError("auth.type=basic 时必须指定 username / password")
        raw = f"{user}:{password}".encode("utf-8")
        headers[self.spec.get("header", "Authorization")] = "Basic " + base64.b64encode(raw).decode()

    def describe(self) -> str:
        return "Authorization: Basic <base64>"


@register
class SignatureAuth(AuthStrategy):
    """HMAC 签名：把时间戳放进待签名串，token 当密钥。

    真实系统里通常还会拼上 method/path/body，这里给出可扩展的骨架。
    """

    type = "signature"

    def apply(self, *, headers, params, cookies, token, method, url) -> None:
        algorithm = self.spec.get("algorithm", "sha256")
        if not hasattr(hashlib, algorithm):
            raise ConfigError(f"signature 策略不支持算法 '{algorithm}'")
        timestamp = str(int(time.time()))
        payload = f"{method}|{url}|{timestamp}"
        digest = hmac.new(str(token).encode("utf-8"), payload.encode("utf-8"),
                          getattr(hashlib, algorithm)).hexdigest()
        headers[self.spec.get("ts_header", "X-Timestamp")] = timestamp
        headers[self.spec.get("header", "X-Signature")] = digest

    def describe(self) -> str:
        return f"HMAC-{self.spec.get('algorithm', 'sha256').upper()} 签名头"
