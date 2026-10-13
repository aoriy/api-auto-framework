"""商品域页面对象。公开接口，游客即可访问。"""
from __future__ import annotations

from apis.base_api import BaseApi


class ProductApi(BaseApi):
    """商品相关接口，不需要登录。"""

    role = "guest"
    prefix = "/products"

    def list(self, **filters):
        return self.get("", params=self.ctx.interpolate(filters))

    def detail(self, product_id):
        return self.get(f"/{product_id}")
