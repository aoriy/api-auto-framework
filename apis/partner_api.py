"""开放平台页面对象：Authorization 可选入口的示例。

整个项目以会话 Cookie 为主，这个类存在的意义是证明**可选入口**同样是
声明式的：只改账号表里的 auth.type，页面类和用例写法完全一致。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class PartnerApi(BaseApi):
    """开放平台接口，走 Authorization: Bearer <token>。"""

    account = "partner_bot"
    prefix = ""

    def ping(self):
        return self.get("/partner/ping")
