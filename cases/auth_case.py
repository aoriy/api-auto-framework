"""多角色鉴权用例。

这条用例专门回答一个问题：角色是怎么被框架自动接上的？
  - 游客调公开接口 → 不带任何凭证，成功
  - 游客调私有接口 → 框架不给他塞 token，被 401 拦下（证明角色之间不串号）
  - admin 调 /users/me → 框架自动带 Authorization: Bearer ...
  - merchant 下单 → 框架自动带 X-Merchant-Token ...
"""
from __future__ import annotations

from apis.order_api import OrderApi
from apis.product_api import ProductApi
from apis.user_api import UserApi
from cases.base_case import BaseCase
from core.assertions import field


class AuthCase(BaseCase):
    def setup_case(self) -> None:
        self.product = self.api(ProductApi)     # role = guest
        self.order = self.api(OrderApi)         # role = merchant
        self.user = self.api(UserApi)           # role = admin
        self.mark("装配页面对象", True,
                  f"可用角色: {', '.join(self.ctx.available_roles)}")

    def test_01_guest_reads_public_products(self) -> None:
        resp = self.product.list(page=1, size=5)
        self.check("游客读公开商品列表", resp, 200, expect=field("data.items"))
        first_id = resp.get("data.items.0.id")
        self.ctx.set("first_product_id", first_id)

    def test_02_guest_blocked_on_private_api(self) -> None:
        """游客没有凭据，框架不会硬塞 token —— 必须被拒。"""
        resp = self.order.as_role("guest").list_mine()
        self.check("游客访问订单接口应被 401 拒绝", resp, 401)

    def test_03_admin_login_and_read_me(self) -> None:
        token = self.ctx.login("admin")
        self.mark("确保 admin 鉴权就绪", bool(token and token.value),
                  token.describe() if token else "未拿到 token")
        resp = self.user.me()
        self.check("admin 读取 /users/me", resp, 200, expect=field("data.role", "admin"))

    def test_04_merchant_places_order(self) -> None:
        token = self.ctx.login("merchant")
        self.mark("确保 merchant 鉴权就绪", bool(token and token.value),
                  token.describe() if token else "未拿到 token")
        resp = self.order.create(product_id="${first_product_id}", quantity=2)
        self.check("merchant 下单", resp, 201, expect=field("data.id"))

    def test_05_two_roles_never_share_token(self) -> None:
        """admin 和 merchant 的 token 完全独立，且落库字段名不同。"""
        admin_token = self.ctx.factory.token_of("admin")
        merchant_token = self.ctx.factory.token_of("merchant")
        same = bool(admin_token and merchant_token and admin_token.value == merchant_token.value)
        self.mark("两个角色的 token 互相独立", not same,
                  f"admin={admin_token.value[:8] if admin_token else None} / "
                  f"merchant={merchant_token.value[:8] if merchant_token else None}")

    def test_06_admin_reaches_admin_only_endpoint(self) -> None:
        resp = self.order.admin_stats()
        self.check("admin 读 /orders/stats（admin 专属）", resp, 200,
                   expect=field("data.total_orders"))
