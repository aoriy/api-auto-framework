"""账号与角色校验用例：确认四个账号已创建，且角色分配正确。

被校验的对象（本次新增）：

    test1    → creator         流程创建人
    test34   → sre_expert      SRE 专家
    test38   → sre_expert      SRE 专家
    test666  → stg_maintainer  维护 STG

「创建成功」不靠口头确认，而是三层实证：
  1. 客户端账号表里有这四个账号（config/accounts.session.yaml）
  2. **服务端注册表**里有这四个账号且角色正确（GET /registry/accounts）
  3. 每个账号能用各自口令登录，并拿到与预期一致的角色（GET /me）

「角色分配正确」同样实证：每个角色只能访问自己的接口，越权一律 403，
并且一条完整流程（创建 → 评审 → 部署）必须由三个不同账号接力完成。
"""
from __future__ import annotations

from apis.flow_api import FlowApi
from cases.base_case import BaseCase
from core.assertions import field

EXPECTED_ROLES = {
    "test1": "creator",
    "test34": "sre_expert",
    "test38": "sre_expert",
    "test666": "stg_maintainer",
}

ROLE_LABELS = {
    "creator": "流程创建人",
    "sre_expert": "SRE 专家",
    "stg_maintainer": "维护 STG",
}


class AccountRolesCase(BaseCase):
    def setup_case(self) -> None:
        self.flow = self.api(FlowApi)          # 页面类默认账号 test1（流程创建人）
        self.mark("装配变更流程页面对象", True,
                  f"默认账号 {self.flow.principal}；账号表共 {len(self.ctx.available_accounts)} 个账号")

    # ---------------- 01 账号是否真的存在 ----------------
    def test_01_four_accounts_exist(self) -> None:
        # ① 客户端账号表
        available = set(self.ctx.available_accounts)
        missing = sorted(n for n in EXPECTED_ROLES if n not in available)
        self.mark("账号表已登记 test1 / test34 / test38 / test666", not missing,
                  f"缺失: {missing or '无'}；账号表共 {len(available)} 个账号")

        # ② 服务端注册表 —— 这才是「系统里有没有这个账号」的权威答案
        resp = self.flow.get("/registry/accounts")
        self.check("读取服务端账号注册表", resp, 200, expect=field("data.total"))
        server_side = {a["account"]: a["role"]
                       for a in (resp.get("data.accounts") or [])}
        for name, role in EXPECTED_ROLES.items():
            self.mark(f"服务端已创建账号 {name}，角色 {role}", server_side.get(name) == role,
                      f"服务端实际角色={server_side.get(name)!r}")

    # ---------------- 02 能否登录 + 角色是否一致 ----------------
    def test_02_each_account_logs_in_with_expected_role(self) -> None:
        summary = []
        for name, role in EXPECTED_ROLES.items():
            api = self.api(FlowApi, account=name)
            resp = api.me()
            self.check(f"{name} 用独立口令登录并读取 /me", resp, 200,
                       expect=field("data.account", name))
            self.check(f"{name} 的角色为 {role}（{ROLE_LABELS[role]}）", resp, 200,
                       expect=field("data.role", role))
            summary.append(f"{name}={resp.get('data.role')}")
        self.mark("四个账号全部登录成功且角色与预期一致", len(summary) == 4,
                  " / ".join(summary))

    # ---------------- 03 创建人专属接口 ----------------
    def test_03_only_creator_can_create_change(self) -> None:
        resp = self.flow.create_change("网关实例扩容")
        self.check("test1（流程创建人）发起变更流程", resp, 201,
                   expect=field("data.creator", "test1"))
        self.ctx.set("change_id", resp.get("data.id"))

        for name in ("test34", "test38", "test666"):
            denied = self.flow.as_account(name).create_change(f"{name} 越权发起")
            self.check(f"{name} 发起变更应被 403 拒绝", denied, 403)

    # ---------------- 04 SRE 专家专属接口 ----------------
    def test_04_only_sre_expert_can_review(self) -> None:
        for name in ("test1", "test666"):
            denied = self.flow.as_account(name).sre_review("${change_id}")
            self.check(f"{name} 评审变更应被 403 拒绝", denied, 403)

        resp = self.flow.as_account("test34").sre_review("${change_id}")
        self.check("test34（SRE 专家）评审通过", resp, 200,
                   expect=field("data.reviewed_by", "test34"))

    # ---------------- 05 维护 STG 专属接口 + 顺序约束 ----------------
    def test_05_stg_deploy_needs_review_first_and_right_role(self) -> None:
        fresh = self.flow.as_account("test1").create_change("调整缓存参数")
        fresh_id = fresh.get("data.id")
        early = self.flow.as_account("test666").stg_deploy(fresh_id)
        self.check("未通过评审的流程不能部署（409）", early, 409)

        for name in ("test1", "test34"):
            denied = self.flow.as_account(name).stg_deploy("${change_id}")
            self.check(f"{name} 部署变更应被 403 拒绝", denied, 403)

        ok = self.flow.as_account("test666").stg_deploy("${change_id}")
        self.check("test666（维护 STG）部署已评审流程", ok, 200,
                   expect=field("data.deployed_by", "test666"))

    # ---------------- 06 同角色不同账号权限一致 ----------------
    def test_06_both_sre_accounts_have_the_same_role_power(self) -> None:
        fresh = self.flow.create_change("滚动重启网关节点")
        fresh_id = fresh.get("data.id")
        resp = self.flow.as_account("test38").sre_review(fresh_id)
        self.check("test38（另一位 SRE 专家）同样可以评审", resp, 200,
                   expect=field("data.reviewed_by", "test38"))

    # ---------------- 07 账号之间会话隔离 ----------------
    def test_07_sessions_are_isolated_across_accounts(self) -> None:
        fingerprints = {}
        for name in EXPECTED_ROLES:
            resp = self.api(FlowApi, account=name).whoami()
            self.check(f"{name} 会话指纹可读", resp, 200, expect=field("data.session_prefix"))
            fingerprints[name] = (resp.get("data.session_prefix"), resp.get("data.csrf_prefix"))

        self.mark("四个账号的 SESSION 与 CSRF 两两不同",
                  len(set(fingerprints.values())) == 4,
                  " / ".join(f"{k}={v[0]}" for k, v in fingerprints.items()))

        sessions = [self.ctx.session(n) for n in EXPECTED_ROLES]
        self.mark("四个账号持有不同的 requests.Session 实例",
                  len({id(s) for s in sessions}) == 4, f"ids={[id(s) for s in sessions]}")

    # ---------------- 08 三方协同跑通一条完整流程 ----------------
    def test_08_full_change_flow_spans_three_roles(self) -> None:
        created = self.flow.create_change("发布 v2.7.0 到 STG")
        cid = created.get("data.id")
        self.check("① test1 发起变更", created, 201, expect=field("data.status", "created"))

        reviewed = self.flow.as_account("test34").sre_review(cid)
        self.check("② test34 评审通过", reviewed, 200, expect=field("data.status", "sre_approved"))

        deployed = self.flow.as_account("test666").stg_deploy(cid)
        self.check("③ test666 部署到 STG", deployed, 200,
                   expect=field("data.status", "deployed_stg"))

        detail = self.flow.change_detail(cid)
        self.check("流程详情可回查且记录了部署责任人", detail, 200,
                   expect=field("data.deployed_by", "test666"))

        actors = {created.get("data.creator"), reviewed.get("data.reviewed_by"),
                  deployed.get("data.deployed_by")}
        self.mark("同一条流程由三个不同账号接力完成", actors == {"test1", "test34", "test666"},
                  f"创建={created.get('data.creator')} "
                  f"评审={reviewed.get('data.reviewed_by')} "
                  f"部署={deployed.get('data.deployed_by')}")
