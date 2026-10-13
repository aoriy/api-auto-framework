"""完成页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class CompletedPageApi(BaseApi):
    """完成页面：关闭单据，流程结束。"""

    account = "test1"        # ★ 替换点②：本页面默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_completion(self, case_id, remark="", **extra):
        """完成 / 关闭单据。"""
        payload = {"remark": remark, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/complete"),
                         json=self.ctx.interpolate(payload))

    def case_detail(self, case_id):
        """流程详情回查（非模板接口，用于用例收尾核对）。"""
        return self.get(self.ctx.interpolate(f"/cases/{case_id}"))
