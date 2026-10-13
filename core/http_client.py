"""HTTP 客户端：所有接口请求的唯一出口。

页面类不碰鉴权、不碰 base_url、不碰重试，全部在这里收口：
  1. 拼 URL（base_url + path）
  2. 调 token provider 拿该账号的凭证（懒登录，缓存在凭证仓库）
  3. 交给 AuthStrategy 的 prepare() 补 CSRF 等会话信息，再 apply() 注入鉴权
  4. 超时、重试、401 自动重登、日志

**一个账号一个实例，一个实例一个 requests.Session。**
这是「请求间账号隔离」的物理保证：CookieJar 挂在 Session 上，
实例不共享，Cookie 就不可能串。
"""
from __future__ import annotations

import time
from typing import Callable

import requests

from core.auth.base import AuthStrategy
from core.exceptions import ApiError
from core.response import ApiResponse


class HttpClient:
    def __init__(self, *, base_url: str, account: str = "anonymous", timeout: float = 10.0,
                 retries: int = 2, backoff: float = 0.3, verify: bool = False,
                 logger=None, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.account = account
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.verify = verify
        self.log = logger
        # 该账号独占的会话：Cookie 隔离就靠它
        self.session = session or requests.Session()
        self._strategy: AuthStrategy | None = None
        self._token_provider: Callable[[], str | None] | None = None
        self._on_unauthorized: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # 由 SessionFactory 注入，页面类永远不需要调用
    # ------------------------------------------------------------------
    def bind_auth(self, strategy: AuthStrategy, token_provider: Callable[[], str | None],
                  on_unauthorized: Callable[[], None] | None = None) -> "HttpClient":
        self._strategy = strategy
        self._token_provider = token_provider
        self._on_unauthorized = on_unauthorized
        return self

    @property
    def auth_type(self) -> str:
        return self._strategy.type if self._strategy else "unbound"

    @property
    def role(self) -> str:
        """兼容旧字段名：现在承载的是账号名。"""
        return self.account

    @property
    def cookies(self) -> dict:
        """该账号当前 CookieJar 的快照（只读，用于断言隔离与排查串号）。"""
        return {c.name: c.value for c in self.session.cookies}

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None, json=None,
                data=None, headers: dict | None = None, files=None, auth: bool = True,
                timeout: float | None = None, raw_url: bool = False, **kwargs) -> ApiResponse:
        url = path if raw_url else self._join(path)
        method = method.upper()
        attempt = 0

        while True:
            attempt += 1
            send_headers = dict(headers or {})
            send_params = dict(params or {})
            send_cookies: dict = {}

            if auth and self._strategy is not None:
                token = None
                # requires_credential 的账号（cookie / bearer / header …）都要先确保凭证就绪。
                # provider 返回的是「需要注入请求的值」：会话类账号返回 None，因为
                # 它们没有可注入的值 —— 凭证在 CookieJar 里，由 Session 自动带。
                if self._strategy.requires_credential and self._token_provider is not None:
                    token = self._token_provider()
                # prepare 先跑：它能读到本账号 CookieJar 里的 CSRF token
                self._strategy.prepare(session=self.session, headers=send_headers,
                                       method=method, url=url)
                self._strategy.apply(headers=send_headers, params=send_params,
                                     cookies=send_cookies, token=token,
                                     method=method, url=url)

            started = time.perf_counter()
            try:
                raw = self.session.request(
                    method, url,
                    params=send_params or None,
                    json=json,
                    data=data,
                    headers=send_headers or None,
                    cookies=send_cookies or None,
                    files=files,
                    timeout=timeout or self.timeout,
                    verify=self.verify,
                    **kwargs,
                )
            except requests.RequestException as exc:
                if attempt <= self.retries:
                    self._wait(attempt, f"网络异常 {type(exc).__name__}")
                    continue
                raise ApiError(f"{method} {url} 请求失败: {exc}") from exc

            elapsed = round((time.perf_counter() - started) * 1000, 2)
            response = ApiResponse(
                method=method, url=url, status_code=raw.status_code,
                headers=dict(raw.headers), text=raw.text, elapsed_ms=elapsed,
                account=self.account,
                request_kwargs={"params": send_params, "json": json, "data": data,
                                "headers": send_headers},
            )

            # 401 只重登一次，避免凭证配置错误时死循环。
            # 只有「本来就要凭证」的账号才值得重登：
            # 游客（none）或 Basic 账号收到 401 是权限/配置问题，重登没意义。
            if (raw.status_code == 401 and auth and attempt == 1
                    and self._on_unauthorized and self._strategy is not None
                    and self._strategy.requires_credential):
                if self.log:
                    self.log.warning("账号 %s 收到 401，作废本人会话后重试一次", self.account)
                self._on_unauthorized()
                continue

            if raw.status_code >= 500 and attempt <= self.retries:
                self._wait(attempt, f"服务端 {raw.status_code}")
                continue

            self._trace(response)
            return response

    def get(self, path: str, **kw) -> ApiResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> ApiResponse:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> ApiResponse:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw) -> ApiResponse:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> ApiResponse:
        return self.request("DELETE", path, **kw)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _join(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url

    def _wait(self, attempt: int, reason: str) -> None:
        delay = self.backoff * attempt
        if self.log:
            self.log.warning("第 %d 次尝试失败（%s），%.1fs 后重试", attempt, reason, delay)
        time.sleep(delay)

    def _trace(self, response: ApiResponse) -> None:
        if self.log:
            self.log.debug("← %s", response.summary)

    def __repr__(self) -> str:
        return (f"<HttpClient account={self.account} auth={self.auth_type} "
                f"cookies={len(self.session.cookies)} base={self.base_url}>")
