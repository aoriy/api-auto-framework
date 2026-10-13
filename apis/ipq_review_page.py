"""IPQ 审核页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class IpqReviewPageApi(BaseApi):
    """IPQ 审核页面：对 FAE 汇总结果做 IPQ 侧审核。"""

    account = "test38"       # ★ 替换点②：本页面默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_ipq_review(self, case_id, conclusion="approved", **extra):
        """提交 IPQ 审核结论。"""
        payload = {"conclusion": conclusion, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/ipq-review"),
                         json=self.ctx.interpolate(payload))
