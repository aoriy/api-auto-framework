"""FAE 汇总页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class FaeSummaryPageApi(BaseApi):
    """FAE 汇总页面：汇总专家评审结论，形成 FAE 侧意见。"""

    account = "test1"        # ★ 替换点②：本页面默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_fae_summary(self, case_id, summary="", **extra):
        """提交 FAE 汇总。"""
        payload = {"summary": summary, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/fae-summary"),
                         json=self.ctx.interpolate(payload))
