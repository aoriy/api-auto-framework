"""排查中页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class InvestigatingPageApi(BaseApi):
    """排查中页面：审核通过后进入排查阶段，记录排查过程信息。"""

    account = "test666"      # ★ 替换点②：本页面默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_investigation(self, case_id, owner="", note="", **extra):
        """进入排查 / 更新排查进展。"""
        payload = {"owner": owner, "note": note, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/investigate"),
                         json=self.ctx.interpolate(payload))
