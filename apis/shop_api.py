"""采购域页面对象（会话 Cookie 版）。

演示一个工作流里「不同接口由不同账号执行」的写法：

    self.shop        → 默认账号 buyer_a
    self.shop_b      → 同页面类换成 buyer_b
    self.admin       → 换成 ops_admin

每个实例各持自己的客户端（各带独立 CookieJar），因此 A 的会话永远不会
被 B 的请求带上。方法体里依然**没有任何凭证代码**。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class ShopApi(BaseApi):
    """采购流程相关接口。默认以账号 buyer_a 访问。"""

    account = "buyer_a"
    prefix = ""

    # ---------------- 公开接口 ----------------
    def catalog(self):
        return self.get("/catalog")

    # ---------------- 采购员接口 ----------------
    def place_order(self, product_id, quantity: int = 1, **extra):
        """下单。payload 里的 ${var} 会被上下文自动替换。"""
        payload = {"product_id": product_id, "quantity": quantity, **extra}
        return self.post("/cart/orders", json=self.ctx.interpolate(payload))

    def my_orders(self):
        return self.get("/cart/orders")

    def order_detail(self, order_id):
        return self.get(self.ctx.interpolate(f"/cart/orders/{order_id}"))

    # ---------------- 管理员接口：换账号调用 ----------------
    def approve(self, order_id):
        """审批订单：同一个页面类，切到 ops_admin 账号。"""
        return self.as_account("ops_admin").post(
            self.ctx.interpolate(f"/cart/orders/{order_id}/approve"))

    def admin_overview(self):
        return self.as_account("ops_admin").get("/admin/overview")

    # ---------------- 诊断接口 ----------------
    def whoami(self):
        """返回当前账号的 session/csrf 指纹 —— 用来证明账号之间会话不共享。"""
        return self.get("/debug/whoami")
