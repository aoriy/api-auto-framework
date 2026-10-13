"""页面对象基类。

约定（这是全框架最重要的一条）：
  * 一个业务域 = 一个子类；一个接口 = 一个方法。
  * 子类用 `account` 声明本页面默认用哪个账号访问；
    旧写法 `role` 仍然可用（当 account 未指定时生效）。
  * 方法体里只有业务语义，**禁止出现任何凭证相关代码**——
    鉴权在 `self.client = ctx.client(self.principal)` 这一行就已经自动挂好了。
  * 需要临时换账号，用 `self.as_account("buyer_b")`，它会返回一个新的页面实例，
    不污染默认账号，也不会串号。
  * 子类**不必**声明 `prefix`：URL 公共前缀统一在 config/env/<env>.yaml 的
    api_prefix 里定义，所有未声明的页面对象共用它。只有两种情况才写 prefix：
    页面自带独立前缀写 `prefix = "/xx"`；路径本身就是完整路径写 `prefix = ""`。

关于「账号」与「角色」：
  账号是登录单位（一个账号 = 一套凭据 + 一次会话），角色是账号的业务权限属性。
  同一个页面类可以按需要换账号，例如「采购员 A 下单 → 运营管理员审批」：
      self.as_account("ops_admin").approve(order_id)
"""
from __future__ import annotations

from core.context import Context


class BaseApi:
    account: str | None = None    # 子类覆盖：本页面默认账号
    role: str = "guest"           # 兼容旧写法：account 为 None 时用它

    #: URL 前缀。三态语义必须分清楚，否则会把「顶级路径」的接口拼歪：
    #:   None  —— 未声明 → 回落到环境配置 config/env/<env>.yaml 里的 api_prefix
    #:   ""    —— 明确表示「无前缀」，方法里的 path 就是完整路径（如 /flow/changes）
    #:   "/xx" —— 本页面自带的固定前缀，优先级最高，不受环境配置影响
    prefix: str | None = None

    def __init__(self, ctx: Context, account: str | None = None, role: str | None = None):
        self.ctx = ctx
        chosen = account or role
        if chosen:
            self.account = chosen
        # 页面类没声明 prefix（None）时，统一取环境配置里的公共前缀。
        # 注意读的是 type(self).prefix（类属性），这样多页面共用一个默认值，
        # 又允许个别页面用类属性或 as_prefix() 精确覆盖。
        declared = type(self).prefix
        self.prefix = ctx.api_prefix if declared is None else declared
        # 关键一行：按账号取到「已经会自己带凭证」的客户端
        self.client = ctx.client(self.principal)

    @property
    def principal(self) -> str:
        """本页面对象当前使用的账号名。"""
        return self.account or self.role

    # ---------------- 账号切换 ----------------
    def as_account(self, account: str) -> "BaseApi":
        """以另一个账号访问同一组接口。返回新实例，不影响当前实例。"""
        return type(self)(self.ctx, account=account)

    #: 兼容旧调用名
    def as_role(self, role: str) -> "BaseApi":
        return self.as_account(role)

    # ---------------- 请求薄封装 ----------------
    def _url(self, path: str) -> str:
        return f"{self.prefix}{path}"

    def get(self, path: str, **kw):
        return self.client.get(self._url(path), **kw)

    def post(self, path: str, **kw):
        return self.client.post(self._url(path), **kw)

    def put(self, path: str, **kw):
        return self.client.put(self._url(path), **kw)

    def patch(self, path: str, **kw):
        return self.client.patch(self._url(path), **kw)

    def delete(self, path: str, **kw):
        return self.client.delete(self._url(path), **kw)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} account={self.principal} prefix={self.prefix or '/'}>"
