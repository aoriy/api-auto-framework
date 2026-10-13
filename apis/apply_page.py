"""申请页面（模板）。

★ 这是一个**模板页面对象**，供你把接口替换成真实的。全项目页面类写法完全一致，
   替换时只动下面两处，用例层一行都不用改：

     ①  方法里的 path / 入参             →  你们真实的接口路径与参数
     ②  `account`                        →  这个页面实际由哪个账号操作

★ URL 公共前缀（本组页面统一是 /flow）已上移到环境配置，页面类里不再重复：
     见 config/env/<环境>.yaml 的 api_prefix。换网关 / 换 API 版本前缀时只改那一行，
     8 个页面类一处都不用动。个别页面要自带前缀时才写 `prefix = "/xx"`。

★ 方法体里**不要写任何凭证代码**。鉴权已由 BaseApi.__init__ 里那一行
   `self.client = ctx.client(self.principal)` 按 account 自动挂好；
   需要临时换账号，用 `self.as_account("test34")`，它返回新实例、不串号。

★ 命名约定：`xxx_page.py` / `XxxPageApi` —— 一个页面一个文件，一个接口一个方法。
"""
from __future__ import annotations

from apis.base_api import BaseApi


class ApplyPageApi(BaseApi):
    """申请页面：提交问题处理 / 变更申请，产出后续所有页面共用的单号。"""

    account = "test1"        # ★ 替换点②：本页面的默认操作账号
    # 前缀不在此声明：统一取环境配置 config/env/<环境>.yaml 的 api_prefix

    # ==========================================================
    # 【模板接口】—— 复制这个方法的形状，替换成你的真实接口即可
    # ==========================================================
    def submit_application(self, title, **extra):
        """提交申请。

        真实接口通常还需要申请人、产品型号、问题描述、期望完成时间等字段，
        直接通过 **extra 传进来即可，例如：
            self.apply_page.submit_application("XX 模块异常", product="A100", priority="P1")

        返回值统一是 core.response.ApiResponse，可用 .status_code / .get("data.id") 读取。
        """
        payload = {"title": title, **extra}
        return self.post("/cases", json=self.ctx.interpolate(payload))

    # ==========================================================
    # 【模板接口 ②】查询申请单列表 —— 演示「同一页面下再新增一条接口」
    # ==========================================================
    def list_applications(self, status=None, page=1, size=20):
        """查询申请单列表。

        和上面那条相比只多了两点，其余约定完全一样：
          * 用 self.get 而不是 self.post；
          * 查询参数放进 params（框架会拼到 URL 上）。

        prefix 已在类上声明，所以这里只写相对路径 "/cases"；
        鉴权同样一行都不用管。
        """
        params = {"page": page, "size": size}
        if status:
            params["status"] = status
        return self.get("/cases", params=self.ctx.interpolate(params))
