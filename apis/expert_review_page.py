"""专家评审页面（模板）。

模板与替换约定见 apis/apply_page.py 顶部说明，这里只列本页面自己的信息。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class ExpertReviewPageApi(BaseApi):
    """专家评审页面：对已提交的申请做专家评审。

    注意 account 与申请页面不同 —— 这正是「同一批接口，不同页面走不同账号」的
    典型场景：申请方 test1 提交，评审方 test34 评审。框架会自动用 test34 自己的
    会话与 CSRF 去调接口，页面方法里依然看不到任何鉴权代码。
    """

    account = "test34"       # ★ 替换点②：本页面默认操作账号（评审方）
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】
    # ==========================================================
    def submit_expert_review(self, case_id, conclusion="pass", **extra):
        """提交专家评审结论。

        case_id 支持跨步骤变量：用例里直接传 "${case_id}" 即可，
        框架在发请求前把它替换成真实值。
        """
        payload = {"conclusion": conclusion, **extra}
        return self.post(self.ctx.interpolate(f"/cases/{case_id}/expert-review"),
                         json=self.ctx.interpolate(payload))
