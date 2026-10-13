"""账号 → 已鉴权 HttpClient 的工厂。

这是整个框架唯一一处把「账号」和「鉴权方式」拼在一起的地方，也是
「账号信息 + 接口信息 = 自动使用该账号的凭证」这条约定的落点。

多账号隔离的三条铁律，全部落在本文件里：

  1. **一个账号一个 HttpClient，一个 HttpClient 一个 requests.Session。**
     CookieJar 挂在 Session 上，实例不共享 → Cookie 不可能串。

  2. **登录必须发生在该账号自己的 Session 上。**
     用 `client.request(..., auth=False)` 发登录请求，Set-Cookie 才会落进
     这个账号专属的 CookieJar。若改用公共 Session 登录，会把 A 的会话
     发给 B —— 这是 Cookie 模式下最隐蔽的串号来源。

  3. **作废会话只作废本人。**
     401 重登只清当前账号的 Session 与缓存，其它账号的会话不受影响。

调用方只需要说 `factory.client("buyer_a")`，拿到的客户端就已经会自己带上
该账号的会话 Cookie，页面类和用例里不会出现任何一行凭证相关代码。
"""
from __future__ import annotations

import time

from core.auth import build_strategy
from core.auth.base import AuthStrategy
from core.exceptions import ApiError, AuthError, RoleNotDefined
from core.http_client import HttpClient
from core.token_store import Token, TokenStore

#: 登录请求允许带上的载荷字段（三选一）
_PAYLOAD_KEYS = ("json", "data", "params")


class SessionFactory:
    def __init__(self, settings, accounts: dict, token_store: TokenStore, logger=None):
        self.settings = settings
        self.accounts = accounts
        self.store = token_store
        self.log = logger
        self._clients: dict[str, HttpClient] = {}
        self._strategies: dict[str, AuthStrategy] = {}

    # ------------------------------------------------------------------
    # 兼容旧命名：roles / role 现在都指账号
    # ------------------------------------------------------------------
    @property
    def roles(self) -> dict:
        return self.accounts

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------
    def client(self, account: str) -> HttpClient:
        """取某个账号的客户端。同一个账号永远拿到同一个实例（复用连接池与 CookieJar）。"""
        if account not in self.accounts:
            raise RoleNotDefined(
                f"未定义的账号 '{account}'，accounts.yaml 里可用: {sorted(self.accounts)}"
            )
        if account in self._clients:
            return self._clients[account]

        spec = self.accounts[account]
        strategy = build_strategy(spec.get("auth"))
        client = HttpClient(
            base_url=self.settings.base_url,
            account=account,
            timeout=self.settings.timeout,
            retries=self.settings.retries,
            backoff=self.settings.backoff,
            verify=self.settings.verify_ssl,
            logger=self.log,
        ).bind_auth(
            strategy,
            # 语义：确保该账号凭证就绪，并返回「需要注入请求的值」。
            # 会话类账号（cookie）没有可注入的值，返回 None —— 凭证在 Session 里。
            token_provider=lambda: self._credential_for(account, strategy),
            on_unauthorized=lambda: self._renew(account, strategy),
        )
        self._strategies[account] = strategy
        self._clients[account] = client
        if self.log:
            self.log.info("装配账号客户端 %-10s 鉴权=%s", account, strategy.describe())
        return client

    def session_of(self, account: str):
        """取某账号的 requests.Session（只读用途：断言隔离、排查串号）。"""
        return self.client(account).session

    def cookies_of(self, account: str) -> dict:
        return self.client(account).cookies

    # ------------------------------------------------------------------
    # 凭证生命周期
    # ------------------------------------------------------------------
    def login(self, account: str, *, force: bool = False) -> Token | None:
        """确保该账号凭证就绪：命中有缓存的凭证就复用，没有才真正登录。

        force=True 时忽略缓存强制重新登录（命令行 --fresh-token 走这条路）。
        无凭证账号（none / basic）返回 None。
        """
        if account not in self.accounts:
            raise RoleNotDefined(f"未定义的账号 '{account}'")
        spec = self.accounts[account]
        strategy = self._strategy_of(account)
        if not strategy.requires_credential:
            return None

        client = self.client(account)
        if force:
            self.store.invalidate(self.settings.env, account)
            strategy.clear_session(client.session)
        cached = self.store.get(self.settings.env, account)
        if cached:
            self._restore(account, strategy, cached, client)
            return cached
        return self._do_login(account, spec, strategy)

    def token_of(self, account: str) -> Token | None:
        """返回该账号当前的凭证快照（可能未登录，返回 None）。"""
        return self.store.peek(self.settings.env, account)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _strategy_of(self, account: str) -> AuthStrategy:
        if account not in self._strategies:
            self.client(account)
        return self._strategies[account]

    def _credential_for(self, account: str, strategy: AuthStrategy) -> str | None:
        """token provider：每次请求前都会问一次，命中缓存几乎零成本。"""
        token = self.login(account)
        if token is None or not strategy.needs_token:
            return None
        return token.value or None

    def _restore(self, account: str, strategy: AuthStrategy, token: Token, client: HttpClient) -> None:
        """把落盘的会话快照灌回该账号的 CookieJar（仅新进程/新会话时才需要）。"""
        if not strategy.uses_session or not token.cookies:
            return
        if client.session.cookies:
            return
        strategy.restore(client.session, token.cookies)
        if self.log:
            self.log.info("账号 %-10s 从缓存恢复会话（%d 个 Cookie）",
                          account, len(token.cookies))

    def _renew(self, account: str, strategy: AuthStrategy) -> None:
        """401 自愈：只作废并重建这一个账号的凭证。"""
        if not strategy.requires_credential:
            return
        client = self.client(account)
        self.store.invalidate(self.settings.env, account)
        strategy.clear_session(client.session)
        self._do_login(account, self.accounts[account], strategy)

    def _do_login(self, account: str, spec: dict, strategy: AuthStrategy) -> Token:
        client = self.client(account)
        login = spec.get("login")
        if not login:
            raise AuthError(
                f"账号 '{account}' 的鉴权方式 '{strategy.type}' 需要凭证，但没有配置 login"
            )

        method = str(login.get("method", "POST")).upper()
        path = login.get("path")
        if not path:
            raise AuthError(f"账号 '{account}' 的 login 缺少 path")

        payload_from = next((k for k in _PAYLOAD_KEYS if login.get(k) is not None), None)
        kwargs: dict = {}
        if payload_from:
            kwargs[payload_from] = login[payload_from]
        if login.get("headers"):
            kwargs["headers"] = login["headers"]

        # 重登前先清空该账号自己的会话，避免旧 Cookie 干扰新会话
        strategy.clear_session(client.session)

        if self.log:
            self.log.info("登录账号 %-10s %s %s", account, method, path)

        try:
            # ★ 关键一行：走该账号自己的 session，且 auth=False（不注入鉴权，避免死循环）。
            #   这样服务端下发的 Set-Cookie 才会落进这个账号专属的 CookieJar。
            resp = client.request(method, path, auth=False, **kwargs)
        except ApiError as exc:
            raise AuthError(f"账号 '{account}' 登录请求失败: {exc}") from exc

        if not resp.ok:
            raise AuthError(f"账号 '{account}' 登录失败 HTTP {resp.status_code}: {resp.text[:200]}")

        cookies = strategy.snapshot(client.session) if strategy.uses_session else {}
        value = strategy.extract(resp) if strategy.needs_token else ""
        expires_in = strategy.extract_expires(resp)
        token = Token(
            account=account,
            env=self.settings.env,
            value=value,
            cookies=cookies,
            expires_at=(time.time() + expires_in) if expires_in else 0.0,
            refresh_token=(resp.get(strategy.refresh_path) if strategy.refresh_path else None),
        )
        self.store.set(token)

        if self.log:
            if cookies:
                self.log.info("账号 %-10s 登录成功，会话语境已建立（%d 个 Cookie）",
                              account, len(cookies))
            else:
                self.log.info("账号 %-10s 登录成功，凭证有效期 %ss", account, expires_in)
        return token
