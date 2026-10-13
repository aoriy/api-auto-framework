"""多账号工作流用例：一条流程里不同接口由不同账号执行。

这是「账号变更」而非「角色变更」的典型场景：
    采购员 A 下单  →  采购员 B 下单  →  运营管理员审批 A 的单  →  管理员看全站

验证目标（每一步都对着一个真实风险）：
  1. 未登录访客没有凭证，只能读公开接口
  2. 三个账号各自持有独立的 requests.Session（CookieJar 不共享）
  3. 写操作自动回填与**当前账号**绑定的 CSRF token
  4. 同角色不同账号（buyer_a / buyer_b）互相看不到对方的数据
  5. 换账号只需换页面实例，不需要「先登录再切回」这种顺序纪律
  6. 脏 CSRF（A 的 token 配 B 的 session）必须被拒 —— 切账号最容易漏的一步
  7. 一个账号会话失效时，只重登它自己，不波及其它账号
  8. Authorization 作为可选入口依然可用，且不产生任何会话 Cookie
"""
from __future__ import annotations

from apis.partner_api import PartnerApi
from apis.shop_api import ShopApi
from cases.base_case import BaseCase
from core.assertions import field

_ACCOUNTS = ("buyer_a", "buyer_b", "ops_admin")


class MultiAccountFlowCase(BaseCase):
    def setup_case(self) -> None:
        # 同一个页面类，按账号取到多个互相独立的实例
        self.guest = self.api(ShopApi, account="guest_web")   # 无凭证
        self.shop_a = self.api(ShopApi)                       # 页面类默认账号 buyer_a
        self.shop_b = self.api(ShopApi, account="buyer_b")
        self.shop_admin = self.api(ShopApi, account="ops_admin")
        self.partner = self.api(PartnerApi)                   # Bearer 可选入口
        self.mark("装配页面对象", True,
                  f"可用账号: {', '.join(self.ctx.available_accounts)}")

    # ---------------- 01 访客 ----------------
    def test_01_guest_reads_catalog_without_credential(self) -> None:
        resp = self.guest.catalog()
        self.check("未登录访客读公开目录", resp, 200, expect=field("data.items"))
        self.mark("访客没有拿到任何 Cookie",
                  not self.ctx.cookies("guest_web"),
                  f"cookies={sorted(self.ctx.cookies('guest_web'))}")
        self.ctx.set("sku", resp.get("data.items.0.id"))

    # ---------------- 02 会话隔离 ----------------
    def test_02_three_accounts_hold_isolated_sessions(self) -> None:
        fingerprints = {}
        for name, api in (("buyer_a", self.shop_a), ("buyer_b", self.shop_b),
                          ("ops_admin", self.shop_admin)):
            resp = api.whoami()
            self.check(f"{name} 建立会话并识别身份", resp, 200,
                       expect=field("data.account", name))
            fingerprints[name] = resp.get("data.session_prefix")

        self.mark("三个账号的 SESSION 互不相同",
                  len(set(fingerprints.values())) == 3,
                  " / ".join(f"{k}={v}" for k, v in fingerprints.items()))

        sessions = [self.ctx.session(name) for name in _ACCOUNTS]
        self.mark("三个账号持有不同的 requests.Session 实例",
                  len({id(s) for s in sessions}) == 3,
                  f"ids={[id(s) for s in sessions]}")

        self.mark("每个账号的 CookieJar 各有一份 SESSION + csrftoken",
                  all(sorted(self.ctx.cookies(n)) == ["SESSION", "csrftoken"]
                      for n in _ACCOUNTS),
                  str({n: sorted(self.ctx.cookies(n)) for n in _ACCOUNTS}))

    # ---------------- 03 账号 A 下单 ----------------
    def test_03_buyer_a_places_order(self) -> None:
        resp = self.shop_a.place_order(product_id="${sku}", quantity=2)
        self.check("buyer_a 下单（写操作，自动回填 CSRF）", resp, 201,
                   expect=field("data.buyer", "buyer_a"))
        self.ctx.set("order_a", resp.get("data.id"))
        self.ctx.set("csrf_a", self.ctx.cookies("buyer_a").get("csrftoken"))

    # ---------------- 04 账号 B 下单 ----------------
    def test_04_buyer_b_places_order(self) -> None:
        resp = self.shop_b.place_order(product_id="${sku}", quantity=3)
        self.check("buyer_b 下单（同一页面类，换账号）", resp, 201,
                   expect=field("data.buyer", "buyer_b"))
        self.ctx.set("order_b", resp.get("data.id"))
        self.mark("两个账号的 CSRF token 不同",
                  self.ctx.get("csrf_a") != self.ctx.cookies("buyer_b").get("csrftoken"),
                  "csrftoken 与 session 绑定，换账号必须换")

    # ---------------- 05 同角色不同账号互不可见 ----------------
    def test_05_buyer_a_sees_only_his_orders(self) -> None:
        resp = self.shop_a.my_orders()
        self.check("buyer_a 查询自己的订单", resp, 200, expect=field("data.items"))
        buyers = {o.get("buyer") for o in (resp.get("data.items") or [])}
        self.mark("buyer_a 的列表里不含 buyer_b 的数据",
                  buyers <= {"buyer_a"}, f"buyers={sorted(buyers)}")
        self.mark("A / B 的订单号不同",
                  self.ctx.get("order_a") != self.ctx.get("order_b"),
                  f"{self.ctx.get('order_a')} vs {self.ctx.get('order_b')}")

    # ---------------- 06 跨账号协作：管理员审批 A 的单 ----------------
    def test_06_admin_approves_buyer_a_order(self) -> None:
        # 页面方法内部用 self.as_account("ops_admin") 换账号，返回新实例，不改默认账号
        resp = self.shop_a.approve("${order_a}")
        self.check("ops_admin 审批 buyer_a 的订单", resp, 200,
                   expect=field("data.status", "approved"))
        self.mark("审批人记录为 ops_admin",
                  resp.get("data.approved_by") == "ops_admin",
                  f"approved_by={resp.get('data.approved_by')}")
        self.mark("borrower 页面对象的默认账号没有被污染",
                  self.shop_a.principal == "buyer_a", self.shop_a.principal)

    # ---------------- 07 越权拦截 ----------------
    def test_07_buyer_a_blocked_on_admin_endpoint(self) -> None:
        resp = self.shop_a.get("/admin/overview")
        self.check("buyer_a 访问管理员接口应被 403 拒绝", resp, 403)

    def test_08_buyer_b_cannot_read_buyer_a_order(self) -> None:
        resp = self.shop_b.order_detail("${order_a}")
        self.check("buyer_b 读取 buyer_a 的订单应被 403 拒绝", resp, 403)

    # ---------------- 09 脏 CSRF：切账号最容易漏的一步 ----------------
    def test_09_stale_csrf_is_rejected(self) -> None:
        """拿 buyer_a 的 CSRF 配 buyer_b 的 SESSION，必须被服务端拒绝。

        这里故意 auth=False 绕过框架的 CSRF 自动回填，模拟
        「换了账号但沿用了上一个账号的 CSRF token」这种手写脚本的典型错误。
        """
        stale = self.ctx.get("csrf_a")
        resp = self.shop_b.client.request(
            "POST", "/cart/orders",
            json={"product_id": self.ctx.get("sku"), "quantity": 1},
            headers={"X-CSRFToken": stale},
            auth=False,
        )
        self.check("脏 CSRF（A 的 token + B 的 session）应被 403 拒绝", resp, 403)

    # ---------------- 10 单账号自愈 ----------------
    def test_10_session_expiry_self_heals_per_account(self) -> None:
        probe = self.shop_b.whoami()
        self.check("踢人前确认 buyer_b 会话有效", probe, 200,
                   expect=field("data.session_prefix"))
        before_b = probe.get("data.session_prefix")

        kill = self.shop_admin.put("/admin/session/expire", json={"account": "buyer_a"})
        self.check("管理员强制踢掉 buyer_a 的会话", kill, 200,
                   expect=field("data.expired_sessions"))

        resp = self.shop_a.my_orders()
        self.check("buyer_a 下一次请求自动重登后成功", resp, 200, expect=field("data.items"))

        after_b = self.shop_b.whoami().get("data.session_prefix")
        self.mark("buyer_b 的会话未被波及", before_b == after_b,
                  f"{before_b} -> {after_b}")

    # ---------------- 11 Authorization 可选入口 ----------------
    def test_11_optional_bearer_entry_still_works(self) -> None:
        resp = self.partner.ping()
        self.check("开放平台走 Authorization: Bearer 可选入口", resp, 200,
                   expect=field("data.auth", "bearer"))
        self.mark("Bearer 通道不产生任何会话 Cookie",
                  not self.ctx.cookies("partner_bot"),
                  f"cookies={sorted(self.ctx.cookies('partner_bot'))}")
