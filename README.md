# 接口自动化测试框架（Python + requests / 面向页面 / 多账号）

一套以 `requests` 为底座、按「页面对象（API Object）」组织的接口自动化框架。
核心解决的是两件事：**接口按业务域归类**，以及**多账号鉴权自动注入**。

用例里永远不会出现 `headers["Authorization"] = ...` 这种代码——账号说明白，
剩下的事框架自己接。

> **多账号 / 会话 Cookie**：一套工作流里不同接口由不同账号执行的方案，
> 见 [`docs/多账号认证方案.md`](docs/多账号认证方案.md)（账号与会话一一对应、
> 凭证三层存储、CSRF 绑定、单账号自愈）。
> 运行：`python tools/mock_server_session.py` + `python runner/main.py --env session --suite multi_account`
>
> **账号 vs 角色**：账号是登录单位（一套凭据 + 一次会话），角色只是账号的业务权限属性。
> 同角色可以有多个账号（buyer_a / buyer_b），它们必须各自独立登录、互不串号。
> 旧写法 `role` 仍然可用，作为 `account` 未指定时的回退。

---

## 一、目录结构

```
api-auto-framework/
├── config/                      ← 配置与角色注册表（全项目唯一声明鉴权的地方）
│   ├── env/dev.yaml                 环境配置：base_url / api_prefix / 超时 / 重试 / 日志
│   ├── roles.yaml                   角色定义：怎么登录 + 怎么带鉴权
│   ├── secrets.env                  明文凭据（已 gitignore，绝不进版本库）
│   ├── secrets.env.example          凭据模板，复制成 secrets.env 后填值
│   └── settings.py                  配置装载：读环境文件、展开 ${VAR}、校验缺项
│
├── core/                        ← 框架内核（不含任何业务语义）
│   ├── auth/
│   │   ├── base.py                  AuthStrategy 抽象基类（定义 needs_token / apply / extract）
│   │   ├── strategies.py            7 种内置鉴权策略：none/bearer/header/query/cookie/basic/signature
│   │   └── registry.py              策略注册表 —— 扩展鉴权方式的唯一入口
│   ├── http_client.py               所有请求的唯一出口：拼 URL、注入鉴权、重试、401 自动重登、日志
│   ├── session_factory.py           角色 → 已鉴权客户端。核心约定的落点
│   ├── token_store.py               token 缓存、有效期判断、跨次运行复用
│   ├── context.py                   用例上下文：角色入口 + ${var} 变量传递
│   ├── response.py                  ApiResponse 封装 + 点号路径取值（data.items.0.id）
│   ├── assertions.py                断言回调（status / field / faster_than）
│   ├── exceptions.py                异常体系
│   └── logger.py                    统一日志
│
├── apis/                        ← 页面对象层：一个页面一个类，一个接口一个方法
│   ├── base_api.py                  BaseApi 基类（声明 account，自动挂鉴权）
│   ├── user_api.py / product_api.py / order_api.py    基础业务域
│   ├── shop_api.py / partner_api.py                   会话 Cookie 域 / 开放平台域
│   ├── flow_api.py                                    变更流程域
│   └── *_page.py                    ★ 8 个流程页面模板（申请 / 专家评审 / FAE 汇总 /
│                                      IPQ 审核 / FAE 组长审核 / 排查中 / 排查汇总 / 完成）
│
├── cases/                       ← 用例编排层：一条业务流程一个类
│   ├── base_case.py                 BaseCase：按 test_0N_ 前缀顺序执行 + 结果收集
│   ├── auth_case.py / order_flow_case.py     基础鉴权与订单流程
│   ├── multi_account_flow_case.py            多账号会话隔离
│   ├── account_roles_case.py                 账号与角色校验
│   └── all_pages_case.py            ★ 整体用例：一条用例覆盖 8 个页面
│
├── runner/main.py               ← 主程序：统一调度、汇总报告、退出码
├── suites/regression.yaml       ← 套件定义：声明跑哪些用例、按什么顺序
├── tools/mock_server.py         ← 三角色 mock 服务（纯标准库，用于自测）
├── reports/                     ← JSON 报告输出（自动生成）
└── runtime/                     ← token 缓存（自动生成，可删）
```

---

## 二、分层职责

| 层 | 目录 | 职责 | 不该做什么 |
|---|---|---|---|
| 配置层 | `config/` | 环境参数、角色定义、凭据装载 | 不写业务、不写请求 |
| 内核层 | `core/` | 请求出口、鉴权注入、token 管理、变量传递 | 不知道任何具体接口 |
| 页面对象层 | `apis/` | 描述"这个业务域有哪些接口" | **不碰 token、不碰 base_url** |
| 用例编排层 | `cases/` | 组织业务流程、传数据、写断言 | 不拼 URL、不关心鉴权 |
| 调度层 | `runner/` | 装载 → 装配 → 顺序执行 → 汇总 | 不含任何业务判断 |

依赖方向是单向的：`runner → cases → apis → core`。下层永远不知道上层的存在。

---

## 三、角色与鉴权的调用约定

### 约定 1：角色只在 `config/roles.yaml` 声明一次

一个角色 = 一套凭据引用 + 「怎么登录」+ 「怎么带鉴权」。这里**只写变量名，不写明文**：

```yaml
roles:
  admin:
    login:
      json: { username: ${ADMIN_USERNAME}, password: ${ADMIN_PASSWORD}, role: admin }
    auth:
      type: bearer
      header: Authorization
      prefix: "Bearer "
      token_path: data.access_token     # 从登录响应哪个字段取 token

  merchant:
    login:
      json: { username: ${MERCHANT_USERNAME}, password: ${MERCHANT_PASSWORD}, role: merchant }
    auth:
      type: header
      header: X-Merchant-Token
      token_path: data.token            # 注意：字段位置跟 admin 不同，框架自动适配

  guest:
    login: null
    auth: { type: none }                # 无鉴权
```

### 约定 2：页面类声明 `role`，鉴权自动挂上

```python
class OrderApi(BaseApi):
    role = "merchant"        # ← 只声明这一行
    prefix = "/orders"

    def __init__(self, ctx, role=None):
        super().__init__(ctx, role)
        self.client = ctx.client(self.role)   # ← 拿到的客户端已经会自己带 X-Merchant-Token
```

页面方法里**没有任何 token 代码**：

```python
def create(self, product_id, quantity=1):
    return self.post("", json={"product_id": product_id, "quantity": quantity})
```

### 约定 3：需要换角色时用 `as_role()`，绝不改全局状态

```python
self.order.as_role("guest").list_mine()   # 以游客身份访问同一组接口 → 401
self.order.admin_stats()                  # 内部就是 self.as_role("admin").get("/stats")
```

同一个角色的客户端在 `SessionFactory` 里是单例，不同角色的客户端互相隔离，
**token 永远不会串号**。

### 约定 4：token 生命周期由框架托管

```
页面类发起请求
      ↓
HttpClient 问 token provider：“merchant 的 token 呢？”
      ↓
SessionFactory：缓存有效 → 直接给；无效/不存在 → 用该角色的 login 定义登录一次
      ↓
AuthStrategy.extract() 按 token_path 从登录响应取 token
      ↓
AuthStrategy.apply() 按角色配置注入到 header / query / cookie
      ↓
TokenStore 落盘，后续请求直接复用
```

补充规则：
- 收到 **401** 时自动作废该账号的缓存与会话、重新登录一次、重发原请求。
  上限是**每个请求恰好一次**，由独立的 `auth_retried` 标志保证 —— 不借用 `attempt` 计数，
  因此即使此前已经发生过网络重试或 5xx 重试，401 自愈依然会触发。
- 无鉴权账号（`type: none`）收到 401 **不触发重登**：本来就没带凭证，重登没有意义。
  同理，`auth=False` 的请求（登录自身、联调工具的 `--no-auth`）也不触发，避免递归。
- 重登后仍然是 401：**不再重试**，把 401 原样交给用例（断言照常生效），
  同时打一条 warning，避免「登录其实一直失败」被 401 的表象掩盖。
- 重登只作废**本人**的会话与凭证缓存，其它账号的会话完全不受影响。
- **403 不触发自愈**。403 的语义是「已认证但无权访问」——本框架里越权断言、
  跨账号读取、CSRF 与 session 不匹配都返回 403，重新登录不会改变结论，却会作废一个
  本来有效的会话并多打一次登录请求。真正代表「凭证失效」的是 401；
  若某系统的 403 确实承担了「会话失效」语义，应在后端对齐状态码。
- `--fresh-token` 或 `ctx.login(role, force=True)` 可强制重新登录。
- token 落盘只用于跨次运行复用，删掉 `runtime/` 也能正常工作。

### 约定 5：凭据只有两个入口

- 本地：`config/secrets.env`（已 gitignore）
- CI：同名真实环境变量（`os.environ.setdefault` 保证 CI Secret 优先，不被文件覆盖）

`roles.yaml` 里的 `${ADMIN_USERNAME}` 在装载时被替换；变量缺失直接抛
`ConfigError` 并指出缺哪个，不会静默变成 `None` 打出去。

### 约定 6：跨接口传数据用 `${var}`

```python
self.ctx.set("order_id", resp.get("data.id"))   # 存
resp = self.order.detail("${order_id}")          # 取（URL、payload、params 里都能用）
```

---

## 四、执行顺序的约定

两个层级、两套顺序规则，互不冲突：

**用例之间** —— 由 `suites/regression.yaml` 的列表顺序决定：

```yaml
cases:
  - cases.auth_case:AuthCase
  - cases.order_flow_case:OrderFlowCase     # 按这个顺序跑
```

**用例内部** —— 由方法名字典序决定，所以用 `test_01_` / `test_02_` 前缀控制先后：

```python
def test_01_browse_products(self): ...
def test_02_place_order_by_reference(self): ...
def test_03_read_order_back(self): ...
```

生命周期：`setup_case()` → `test_01_*` → `test_02_*` → … → `teardown_case()`

单个步骤失败**不会中断整条流程**，所有结果都进 `CaseResult`，
一次运行看到全貌，而不是修一个跑一次。

---

## 五、怎么扩展

**新增一个角色** —— 改 1 个文件：

1. `config/secrets.env` 加凭据
2. `config/roles.yaml` 加一段角色定义

页面类里写 `role = "new_role"` 或 `as_role("new_role")` 即可，框架代码零改动。

**新增一种鉴权方式** —— 加 1 个类：

```python
@register
class MyAuth(AuthStrategy):
    type = "myauth"
    def apply(self, *, headers, params, cookies, token, method, url):
        headers["X-My-Auth"] = token
```

`roles.yaml` 里 `type: myauth` 立刻可用。

**新增一个页面（业务域）** —— 加 1 个文件：

```python
class PaymentApi(BaseApi):
    role = "merchant"
    prefix = "/payments"

    def pay(self, order_id, amount):
        return self.post("", json={"order_id": order_id, "amount": amount})
```

**新增一个接口** —— 在已有页面类里加一个方法。一行。

---

## 六、运行

```bash
pip install -r requirements.txt

# 1) 启动 mock 服务（另开一个终端）
python tools/mock_server.py

# 2) 跑回归套件
python runner/main.py --env dev --suite regression

# 常用参数
python runner/main.py --list-roles                  # 只看角色与鉴权方式
python runner/main.py --fresh-token                 # 忽略 token 缓存，强制重新登录
python runner/main.py --role admin --role merchant   # 预热指定角色
```

退出码：`0` 全通过 / `1` 有失败步骤 / `2` 框架级错误（配置缺项、登录失败等）。CI 直接用。

报告落在 `reports/report-<env>-<suite>.json`，含每个用例、每个步骤的状态、耗时、
角色归属，以及运行结束时的 token 缓存状态。

---

## 七、实测结果

对 `tools/mock_server.py`（三角色、三种鉴权形态）实跑：

| 场景 | 结果 |
|---|---|
| 首次运行 `--fresh-token` | 2 用例 / 15 步骤 全部通过，admin 与 merchant 各登录一次 |
| 第二次运行（复用缓存） | 2 用例 / 15 步骤 全部通过，**登录次数 0** |
| 注入伪造 admin token | 触发 401 → 自动重登 → 2 用例 / 15 步骤 仍全部通过 |
| 游客访问订单接口 | 401 被拦，证明角色之间不串号 |
| 商户访问 `/orders/stats` | 403 被拦，证明权限隔离生效 |

---

## 八、账号与角色登记（会话 Cookie 版）

### 账号表就是唯一事实来源

`config/accounts.session.yaml` **同时是客户端账号表和服务端账号注册表**——
`tools/mock_server_session.py` 启动时直接读它（口令取自 `config/secrets.env`），
服务端不再硬编码任何账号。于是：

> **在系统里加一个账号 = 账号表加一段 + `secrets.env` 补一条口令。**

两侧不可能再出现「客户端以为有、服务端其实没有」的漂移。
`GET /registry/accounts` 可随时查看服务端实际持有的账号与角色（**不返回口令**）。

### 已登记账号一览

| 账号 | 角色 | 说明 |
|---|---|---|
| buyer_a / buyer_b | buyer | 采购员（同角色不同账号，会话必须隔离） |
| ops_admin | admin | 运营管理员 |
| guest_web | guest | 未登录访客（无凭证） |
| partner_bot | partner | 开放平台机器人（Bearer，可选入口） |
| **test1** | **creator** | **流程创建人：发起变更流程** |
| **test34** | **sre_expert** | **SRE 专家：评审变更** |
| **test38** | **sre_expert** | **SRE 专家：评审变更** |
| **test666** | **stg_maintainer** | **维护 STG：部署到 STG** |

### 变更流程的角色分工与接口门禁

| 接口 | 允许角色 |
|---|---|
| `POST /flow/changes` | creator |
| `POST /flow/changes/<id>/sre-review` | sre_expert |
| `POST /flow/changes/<id>/stg-deploy` | stg_maintainer（且必须先通过评审，否则 409） |
| `GET /flow/changes`、`GET /flow/changes/<id>` | 任意已登录账号 |

### 怎么校验「账号已创建、角色已分配」

```bash
python tools/mock_server_session.py                            # 另开一个终端
python runner/main.py --env session --suite account_roles
```

用例 `cases/account_roles_case.py` 分三层取证：账号表登记 → 服务端注册表 → 实际登录读角色，
再逐角色试探越权（应 403），最后跑通「创建 → 评审 → 部署」三方接力。实测 **39/39 步骤通过**，
角色权限矩阵：

| 账号 | 服务端角色 | 发起变更 | 评审 | 部署 STG |
|---|---|---|---|---|
| test1 | creator | **201** | 403 | 403 |
| test34 | sre_expert | 403 | **200** | 403 |
| test38 | sre_expert | 403 | **200** | 403 |
| test666 | stg_maintainer | 403 | 403 | **200** |

**换口令**：只改 `config/secrets.env` 里对应的 `TEST*_PASSWORD`，无需动任何代码，
重跑 `account_roles` 套件即可复验。

---

## 九、8 个流程页面模板与整体用例

`apis/` 下按「一个页面一个文件、一个接口一个方法」建好了 8 个**模板页面对象**；
整体用例文件 `cases/all_pages_case.py` 里的一条模板用例把它们按流程顺序串成一条完整链路。
**你要做的只是把模板接口换成真实接口，用例层一行都不用改。**

### 页面 → 模板接口对照表

| # | 页面 | 页面类（文件） | 模板接口方法 | 默认账号 | 模板路径 |
|---|---|---|---|---|---|
| ① | 申请页面 | `ApplyPageApi`<br>`apis/apply_page.py` | `submit_application(title, **extra)` | test1 | `POST /flow/cases` |
| ② | 专家评审页面 | `ExpertReviewPageApi`<br>`apis/expert_review_page.py` | `submit_expert_review(case_id, conclusion)` | test34 | `POST /flow/cases/<id>/expert-review` |
| ③ | FAE 汇总页面 | `FaeSummaryPageApi`<br>`apis/fae_summary_page.py` | `submit_fae_summary(case_id, summary)` | test1 | `POST /flow/cases/<id>/fae-summary` |
| ④ | IPQ 审核页面 | `IpqReviewPageApi`<br>`apis/ipq_review_page.py` | `submit_ipq_review(case_id, conclusion)` | test38 | `POST /flow/cases/<id>/ipq-review` |
| ⑤ | FAE 组长审核页面 | `FaeLeadReviewPageApi`<br>`apis/fae_lead_review_page.py` | `submit_lead_review(case_id, conclusion)` | test34 | `POST /flow/cases/<id>/fae-lead-review` |
| ⑥ | 排查中页面 | `InvestigatingPageApi`<br>`apis/investigating_page.py` | `submit_investigation(case_id, owner, note)` | test666 | `POST /flow/cases/<id>/investigate` |
| ⑦ | 排查汇总页面 | `InvestigationSummaryPageApi`<br>`apis/investigation_summary_page.py` | `submit_investigation_summary(case_id, root_cause, action)` | test666 | `POST /flow/cases/<id>/investigation-summary` |
| ⑧ | 完成页面 | `CompletedPageApi`<br>`apis/completed_page.py` | `submit_completion(case_id, remark)` | test1 | `POST /flow/cases/<id>/complete` |

> 单号由①产出，用例里通过 `self.ctx.set("case_id", ...)` 落进上下文，
> ②~⑧ 用 `"${case_id}"` 引用，框架在发请求前自动替换。

### 替换成真实接口：只动页面类里的两处

| 替换点 | 改什么 | 举例 |
|---|---|---|
| ① 账号 | `account = "test1"` | 改成该页面实际的操作用户名 |
| ② 入参 | 方法签名与 payload | 真实接口要 product/priority/描述等，直接加参数即可 |

> **URL 公共前缀不在这里改。** `/flow` 已上移到 `config/env/<环境>.yaml` 的
> `api_prefix`，全项目只定义一处、8 个页面共用；方法里只写相对路径
> （`self.post("/cases")`），前缀由框架拼。详见**第十节**。

**用例层不用改**——它只认「页面对象 + 方法名」，不认路径、不认账号、不认凭证。
这就是页面对象模式的目的：接口变了，只改一个文件。

### 加一个新页面（三步）

1. 在 `apis/` 建 `xxx_page.py`，照抄任一模板文件的形状；
2. 在 `apis/__init__.py` 里导出这个类；
3. 在 `cases/all_pages_case.py` 的 `PAGE_ORDER` 里补一行 `("XXX 页面", XxxPageApi)`，
   再在模板用例中加一段调用。

> 新建的页面类**不用写 `prefix`**——不写就自动继承环境配置里的 `api_prefix`。

### 怎么校验

```bash
python tools/mock_server_session.py                        # 另开一个终端
python runner/main.py --env session --suite all_pages       # 整体页面套件
```

实测 **16/16 步骤通过**（1 个用例，含新增的列表查询接口），单据从 `applied` 一路推进到
`completed`，8 个阶段的责任人分别落在各自的账号上：

| 阶段 | 页面 | 状态 | 操作账号 |
|---|---|---|---|
| 1 | 申请页面 | applied | test1 |
| 2 | 专家评审页面 | expert_reviewed | test34 |
| 3 | FAE 汇总页面 | fae_summarized | test1 |
| 4 | IPQ 审核页面 | ipq_approved | test38 |
| 5 | FAE 组长审核页面 | lead_approved | test34 |
| 6 | 排查中页面 | investigating | test666 |
| 7 | 排查汇总页面 | investigation_summarized | test666 |
| 8 | 完成页面 | completed | test1 |

**顺序约束也已实测**：跳过②专家评审直接做④IPQ 审核，服务端返回
`409 单据 CASE0008 当前状态 'applied'，不能直接进入「IPQ 审核页面」`。

> mock 侧只做了「已登录 + 阶段顺序」两道门禁，**没有臆造角色权限矩阵**。
> 你们真实的权限规则确定后，把 `CASE_ACTIONS` 里的校验补上角色维度即可
> （参考 `FlowApi` 的 `_require("sre_expert")` 写法）。

---

## 十、页面 URL 公共前缀的统一配置

8 个流程页面的 URL 前缀都是 `/flow`。为避免同一个值在 8 个页面类里各写一遍，
它已经**上移到环境配置**：页面类里不再声明，运行时由框架统一注入。

### 配置在哪

```yaml
# config/env/session.yaml（dev.yaml 同理）
base_url: http://127.0.0.1:18889
api_prefix: /flow          # ← 页面 URL 公共前缀，全项目只定义一处
```

| 位置 | 作用 |
|---|---|
| `config/env/<env>.yaml` 的 `api_prefix` | 页面类的默认前缀，本项目为 `/flow` |
| 环境变量 `API_PREFIX` | 优先级更高，CI 里可临时切换前缀而不改文件 |
| 页面类的 `prefix` 类属性 | 页面自带前缀时覆盖环境默认（如 `OrderApi` 的 `/orders`） |

### 三态语义（必须分清，否则会把顶级路径拼歪）

`apis/base_api.py` 里 `prefix` 有三种取值，含义完全不同：

| 写法 | 含义 | 结果 |
|---|---|---|
| 不写 `prefix` | 未声明 → 取环境默认 | `/flow` + `/cases` → `/flow/cases` |
| `prefix = ""` | 明确「无前缀」，path 本身即完整路径 | `"" + "/flow/changes"` → `/flow/changes` |
| `prefix = "/xx"` | 页面自带前缀，优先级最高 | `/orders` + `/x` → `/orders/x` |

本项目实际分布：8 个流程页面**不写**（用环境默认）；`OrderApi` / `ProductApi` /
`UserApi` **自带**；`FlowApi` / `ShopApi` / `PartnerApi` **写 `""`**。

### 换前缀怎么改

只改 `config/env/<env>.yaml` 里那一行，**8 个页面类一处都不用动**：

```yaml
api_prefix: /api/v2/flow     # 写成 /api/v2/flow、api/v2/flow、/api/v2/flow/ 都行，会自动规范化
```

### 注意与影响范围

- **漏配即大面积 404。** 环境文件里没写 `api_prefix` 时值为空，未声明 `prefix` 的
  页面会直接拼出 `/cases`。启动日志会打印当前生效前缀，为空时给出 WARNING 提示。
- **只影响「未声明 prefix」的页面对象。** 自带前缀和 `prefix = ""` 两类完全不受影响；
  用例层、账号表、鉴权层、mock 服务零改动。
- **先分清这个前缀是「环境差异」还是「接口契约」。** 只有它确实随环境变化
  （网关路径、API 版本不同）时，放环境配置才划算；若所有环境都一样，它本质是应用
  常量，留在代码里也合理。
- **Git Bash 下用 `API_PREFIX=/flow-v2 python ...` 时**，MSYS 会把 `/flow-v2`
  当路径改写。需要加 `MSYS_NO_PATHCONV=1`，或直接改配置文件。
- JSON 报告里会记录本次生效的 `api_prefix`，便于回溯「这次跑的是哪个前缀」。

