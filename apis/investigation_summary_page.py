"""排查汇总页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class InvestigationSummaryPageApi(BaseApi):
    """排查汇总页面：汇总排查结论与根因。"""

    account = "test666"      # ★ 替换点②：本页面默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_investigation_summary(self, case_id, root_cause="", action="", **extra):
        """提交排查汇总（根因 + 处理措施）。"""
        payload = {"root_cause": root_cause, "action": action, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/investigation-summary"),
                         json=self.ctx.interpolate(payload))
