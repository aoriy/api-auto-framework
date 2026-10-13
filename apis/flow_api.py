"""变更流程域页面对象（会话 Cookie 版）。

一条变更流程由三类角色分工完成，**每个角色一个专属接口**：

    流程创建人  creator         发起变更流程      POST /flow/changes
    SRE 专家    sre_expert      评审变更          POST /flow/changes/<id>/sre-review
    维护 STG    stg_maintainer  部署到 STG        POST /flow/changes/<id>/stg-deploy

默认以账号 test1（流程创建人）访问；需要别的角色时用 as_account() 换实例：

    self.flow                               # test1（创建人）
    self.flow.as_account("test34")          # SRE 专家
    self.flow.as_account("test666")         # 维护 STG

方法体里依然**没有任何凭证代码** —— 换账号只是换一个页面实例。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class FlowApi(BaseApi):
    """变更流程相关接口。默认以账号 test1（流程创建人）访问。"""

    account = "test1"
    prefix = ""

    # ---------------- 创建人 ----------------
    def create_change(self, title, **extra):
        """发起一条变更流程。"""
        payload = {"title": title, **extra}
        return self.post("/flow/changes", json=self.ctx.interpolate(payload))

    def my_changes(self):
        return self.get("/flow/changes")

    # ---------------- SRE 专家 ----------------
    def sre_review(self, change_id):
        """评审变更（只有 sre_expert 角色可通过）。"""
        return self.post(self.ctx.interpolate(f"/flow/changes/{change_id}/sre-review"))

    # ---------------- 维护 STG ----------------
    def stg_deploy(self, change_id):
        """把变更部署到 STG（必须先通过 SRE 评审）。"""
        return self.post(self.ctx.interpolate(f"/flow/changes/{change_id}/stg-deploy"))

    # ---------------- 通用 / 诊断 ----------------
    def change_detail(self, change_id):
        return self.get(self.ctx.interpolate(f"/flow/changes/{change_id}"))

    def me(self):
        """当前账号的身份与角色。"""
        return self.get("/me")

    def whoami(self):
        """当前账号的 session / csrf 指纹 —— 用来证明账号之间会话不共享。"""
        return self.get("/debug/whoami")
