"""订单主流程用例：演示跨接口、跨角色的数据传递与越权校验。"""
from __future__ import annotations

from apis.order_api import OrderApi
from apis.product_api import ProductApi
from apis.user_api import UserApi
from cases.base_case import BaseCase
from core.assertions import field


class OrderFlowCase(BaseCase):
    def setup_case(self) -> None:
        self.product = self.api(ProductApi)
        self.order = self.api(OrderApi)         # role = merchant
        self.user = self.api(UserApi)           # role = admin

    def test_01_browse_products(self) -> None:
        resp = self.product.list()
        self.check("查询商品列表", resp, 200, expect=field("data.items"))
        product_id = resp.get("data.items.0.id")
        self.ctx.set("product_id", product_id)
        self.mark("从响应里取出 product_id 存进上下文", product_id is not None,
                  f"product_id={product_id}")

    def test_02_place_order_by_reference(self) -> None:
        """payload 里写 ${product_id}，框架按上下文自动替换成真实值。"""
        resp = self.order.create(product_id="${product_id}", quantity=3)
        self.check("下单（payload 引用 ${product_id}）", resp, 201, expect=field("data.id"))
        self.ctx.set("order_id", resp.get("data.id"))

    def test_03_read_order_back(self) -> None:
        resp = self.order.detail("${order_id}")
        self.check("回查订单详情", resp, 200, expect=field("data.quantity", 3))

    def test_04_admin_reads_global_stats(self) -> None:
        resp = self.order.admin_stats()
        self.check("管理员读全站统计", resp, 200, expect=field("data.total_orders"))

    def test_05_merchant_blocked_on_admin_endpoint(self) -> None:
        """商户用自己身份访问 admin 专属接口，必须 403 —— 权限隔离生效。"""
        resp = self.order.get("/stats")
        self.check("商户访问 /orders/stats 应被 403 拒绝", resp, 403)
