"""整体用例：8 个页面的接口按流程顺序串成一条完整链路。

这是「整体用例文件」—— 一条用例覆盖全部页面，用来验证跨页面串联是否通顺。

  ① 申请页面        提交申请            → applied
  ② 专家评审页面    专家评审            → expert_reviewed
  ③ FAE 汇总页面    FAE 汇总            → fae_summarized
  ④ IPQ 审核页面    IPQ 审核            → ipq_approved
  ⑤ FAE 组长审核页面 组长终审            → lead_approved
  ⑥ 排查中页面      进入排查            → investigating
  ⑦ 排查汇总页面    提交排查汇总        → investigation_summarized
  ⑧ 完成页面        关闭单据            → completed

★ 一条模板用例，就是下面这一个 test_ 方法。你把它复制一份、改改页面方法的入参，
  就是你们自己的业务用例；页面内部换成真实接口后，这个文件**不用改**。

★ 关于账号：本用例**没有写任何切换账号的代码**。每个页面类自己声明了 account
  （申请方 test1 / 评审方 test34 / 排查方 test666 …），框架按页面自动注入对应会话。
  所以「不同页面由不同账号操作」在这里是自动发生的，不需要用例操心。
"""
from __future__ import annotations

from apis import (
    ApplyPageApi,
    CompletedPageApi,
    ExpertReviewPageApi,
    FaeLeadReviewPageApi,
    FaeSummaryPageApi,
    InvestigatingPageApi,
    InvestigationSummaryPageApi,
    IpqReviewPageApi,
)
from cases.base_case import BaseCase
from core.assertions import field


class AllPagesFlowCase(BaseCase):
    """整体用例：一条单据走完 8 个页面。"""

    #: 页面执行顺序（与业务流程一致），同时用于自检「8 个页面是否都装上了」
    PAGE_ORDER = [
        ("申请页面", ApplyPageApi),
        ("专家评审页面", ExpertReviewPageApi),
        ("FAE 汇总页面", FaeSummaryPageApi),
        ("IPQ 审核页面", IpqReviewPageApi),
        ("FAE 组长审核页面", FaeLeadReviewPageApi),
        ("排查中页面", InvestigatingPageApi),
        ("排查汇总页面", InvestigationSummaryPageApi),
        ("完成页面", CompletedPageApi),
    ]

    # ---------------- 装配 ----------------
    def setup_case(self) -> None:
        # 一个页面一个实例；实例内部已按各自 account 挂好鉴权客户端
        self.apply_page = self.api(ApplyPageApi)
        self.expert_review_page = self.api(ExpertReviewPageApi)
        self.fae_summary_page = self.api(FaeSummaryPageApi)
        self.ipq_review_page = self.api(IpqReviewPageApi)
        self.fae_lead_review_page = self.api(FaeLeadReviewPageApi)
        self.investigating_page = self.api(InvestigatingPageApi)
        self.investigation_summary_page = self.api(InvestigationSummaryPageApi)
        self.completed_page = self.api(CompletedPageApi)

        pages = self.page_objects()
        self.mark("装配 8 个页面对象", len(pages) == 8,
                  " / ".join(f"{label}={api.principal}" for label, api in pages))

    def page_objects(self) -> list[tuple[str, object]]:
        """按业务流程顺序返回 (页面名, 页面对象)。"""
        return [(label, getattr(self, self._attr_of(api_cls)))
                for label, api_cls in self.PAGE_ORDER]

    @staticmethod
    def _attr_of(api_cls) -> str:
        """ApplyPageApi → apply_page（用于把类名映射到 setup 里的实例属性名）。"""
        name = api_cls.__name__
        body = name[:-3] if name.endswith("Api") else name      # 去掉 Api 后缀
        out = []
        for i, ch in enumerate(body):
            if ch.isupper() and i:
                out.append("_")
            out.append(ch.lower())
        return "".join(out)

    # ==========================================================
    # 【模板用例】—— 一条用例覆盖全部 8 个页面
    # ==========================================================
    def test_01_full_pages_flow(self) -> None:
        """模板用例：8 个页面接口按顺序全跑一遍。"""

        # ---------- ① 申请页面 ----------
        created = self.apply_page.submit_application(
            "A100 模块上电异常", product="A100", priority="P1")
        self.check("① 申请页面 · 提交申请", created, 201,
                   expect=field("data.status", "applied"))
        case_id = created.get("data.id")
        self.ctx.set("case_id", case_id)
        self.mark("申请单号写入上下文，供后续页面用 ${case_id} 引用", bool(case_id),
                  f"case_id={case_id}")

        # ---------- ② 专家评审页面（默认账号 test34，与申请方不同） ----------
        reviewed = self.expert_review_page.submit_expert_review(
            "${case_id}", conclusion="pass", comment="现象可复现，建议进入汇总")
        self.check("② 专家评审页面 · 提交评审", reviewed, 200,
                   expect=field("data.status", "expert_reviewed"))

        # ---------- ③ FAE 汇总页面 ----------
        fae = self.fae_summary_page.submit_fae_summary(
            "${case_id}", summary="初步定位为电源时序问题")
        self.check("③ FAE 汇总页面 · 提交汇总", fae, 200,
                   expect=field("data.status", "fae_summarized"))

        # ---------- ④ IPQ 审核页面 ----------
        ipq = self.ipq_review_page.submit_ipq_review(
            "${case_id}", conclusion="approved", comment="无明显品质风险")
        self.check("④ IPQ 审核页面 · 提交审核", ipq, 200,
                   expect=field("data.status", "ipq_approved"))

        # ---------- ⑤ FAE 组长审核页面 ----------
        lead = self.fae_lead_review_page.submit_lead_review(
            "${case_id}", conclusion="approved")
        self.check("⑤ FAE 组长审核页面 · 提交终审", lead, 200,
                   expect=field("data.status", "lead_approved"))

        # ---------- ⑥ 排查中页面 ----------
        investigating = self.investigating_page.submit_investigation(
            "${case_id}", owner="test666", note="开始排查电源板")
        self.check("⑥ 排查中页面 · 进入排查", investigating, 200,
                   expect=field("data.status", "investigating"))

        # ---------- ⑦ 排查汇总页面 ----------
        investigation = self.investigation_summary_page.submit_investigation_summary(
            "${case_id}", root_cause="电容批次不良", action="更换物料并加严来料检验")
        self.check("⑦ 排查汇总页面 · 提交排查汇总", investigation, 200,
                   expect=field("data.status", "investigation_summarized"))

        # ---------- ⑧ 完成页面 ----------
        completed = self.completed_page.submit_completion(
            "${case_id}", remark="已验证通过，关闭单据")
        self.check("⑧ 完成页面 · 关闭单据", completed, 200,
                   expect=field("data.status", "completed"))

        # ---------- 收尾核对：8 个阶段是否都真实留下了记录 ----------
        detail = self.completed_page.case_detail("${case_id}")
        self.check("回查单据详情，8 个阶段全部留痕", detail, 200,
                   expect=field("data.stage_count", 8))
        stages = [s["page"] for s in (detail.get("data.stages") or [])]
        self.mark("详情里的阶段顺序与页面顺序一致",
                  stages == [label for label, _ in self.PAGE_ORDER],
                  " → ".join(stages))

        # ---------- 顺序约束：越阶必须被拦住 ----------
        fresh = self.apply_page.submit_application("越阶校验用单据")
        fresh_id = fresh.get("data.id")
        skipped = self.fae_summary_page.submit_fae_summary(fresh_id, "跳过评审直接汇总")
        self.check("未经过②专家评审就做③FAE 汇总，应被 409 拒绝", skipped, 409)

    # ==========================================================
    # 【新增接口的用例】—— 演示「页面里加了新方法后，用例怎么用」
    # ==========================================================
    def test_02_application_search(self) -> None:
        """调用申请页面新增的「查询申请单列表」接口。"""
        listed = self.apply_page.list_applications()
        self.check("申请页面 · 查询申请单列表（不带过滤）", listed, 200,
                   expect=field("data.total"))

        completed = self.apply_page.list_applications(status="completed")
        self.check("申请页面 · 按状态过滤 status=completed", completed, 200)
        items = completed.get("data.items") or []
        self.mark("过滤结果全部为 completed",
                  all(it["status"] == "completed" for it in items),
                  f"全部 {listed.get('data.total')} 张，其中 completed {len(items)} 张")
