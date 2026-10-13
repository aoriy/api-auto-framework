"""用户域页面对象。"""
from __future__ import annotations

from apis.base_api import BaseApi


class UserApi(BaseApi):
    """用户相关接口。默认以管理员身份访问。"""

    role = "admin"
    prefix = "/users"

    def me(self):
        """当前登录人信息（用哪个角色调的，就返回哪个角色）。"""
        return self.get("/me")

    def detail(self, user_id):
        return self.get(f"/{user_id}")

    def update_profile(self, user_id, **fields):
        """更新资料；payload 里的 ${var} 会被上下文自动替换。"""
        return self.put(f"/{user_id}", json=self.ctx.interpolate(fields))

    def list_all(self):
        """用户列表，仅管理员可访问。"""
        return self.get("")

    def as_public(self):
        """以游客身份访问用户域（用于验证越权拦截）。"""
        return self.as_role("guest")
