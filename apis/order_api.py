"""订单域页面对象。

这个类演示两件事：
  1. 同一个页面类默认用一个角色（merchant）访问；
  2. 个别接口需要别的角色时，用 self.as_role("admin") 精确切换，不用改配置。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class OrderApi(BaseApi):
    """订单相关接口。默认以商户身份访问。"""

    role = "merchant"
    prefix = "/orders"

    def create(self, product_id, quantity: int = 1, **extra):
        payload = {"product_id": product_id, "quantity": quantity, **extra}
        return self.post("", json=self.ctx.interpolate(payload))

    def list_mine(self, page: int = 1, size: int = 10):
        return self.get("", params={"page": page, "size": size})

    def detail(self, order_id):
        return self.get(self.ctx.interpolate(f"/{order_id}"))

    def cancel(self, order_id):
        return self.post(f"/{order_id}/cancel")

    def admin_stats(self):
        """全站统计：同一个页面类，临时切到 admin 角色。"""
        return self.as_role("admin").get("/stats")
