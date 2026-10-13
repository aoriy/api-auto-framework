# 框架运行原理 —— 以 OrderFlowCase 为主线

本文回答一个问题：`python runner/main.py --env dev --suite regression` 敲下去之后，一直到
`OrderFlowCase` 的第 5 步跑完，框架内部到底发生了什么。

所有结论都对应仓库里的真实代码，行号以当前版本为准。

---

## 一、模块地图与职责边界

```
api-auto-framework/
├─ runner/main.py          主程序：装配依赖、按套件顺序调度、汇总报告、退出码
├─ suites/*.yaml           用例清单（顺序在此声明）
├─ config/
│   ├─ settings.py         装载 env yaml、解析 ${VAR}、构造 Settings
│   ├─ roles.yaml          角色注册表：一套凭据 + 怎么登录 + 怎么带鉴权
│   ├─ env/dev.yaml        环境地址、超时、重试
│   └─ secrets.env         明文凭据（已 gitignore）
├─ cases/
│   ├─ base_case.py        用例基类：步骤发现、执行、结果收集
│   ├─ auth_case.py        鉴权隔离用例
│   └─ order_flow_case.py  订单主流程用例（本文主线）
├─ apis/
│   ├─ base_api.py         页面对象基类：声明角色 + 请求薄封装
│   ├─ product_api.py      role = guest
│   ├─ order_api.py        role = merchant
│   └─ user_api.py         role = admin
├─ core/
│   ├─ context.py          用例上下文：角色入口 + ${var} 变量总线
│   ├─ session_factory.py  角色 → 已鉴权 HttpClient（唯一拼装点）
│   ├─ http_client.py      所有请求的唯一出口：鉴权注入、重试、401 重登
│   ├─ token_store.py      token 缓存：(env, role) 索引，内存 + 可选落盘
│   ├─ auth/               鉴权策略族（none/bearer/header/query/cookie/basic/signature）
│   ├─ response.py         ApiResponse 封装 + dig() 点号路径取值
│   └─ assertions.py       可复用断言回调（field / status / faster_than / all_of）
└─ tools/mock_server.py    三角色 mock 服务，用来验证框架本身
```

**依赖方向严格单向向下**，下层不知道上层存在：

```
runner  →  cases  →  apis  →  core  →  被测服务
             ↑                  ↑
          suites/*.yaml     config/*.yaml（被 runner 装载后注入 core）
```

判定一个改动该放哪一层，就看它"碰不碰 token"：碰 token 一律进 `core/`，其余各层都不许出现 token 代码。

---

## 二、启动阶段：OrderFlowCase 被实例化之前

`runner/main.py::main()` 依次做五件事：

| 步骤 | 代码 | 说明 |
|---|---|---|
| 1 | `load_settings(args.env)` / `load_roles(settings.roles_file)` | 读 `config/env/dev.yaml` 与 `config/roles.yaml`，把 `${ADMIN_PASSWORD}` 之类的引用解析成真实值 |
| 2 | `TokenStore(settings.token_cache)` | 装载 `runtime/tokens.dev.json`。**命中即复用，此刻不登录** |
| 3 | `SessionFactory(settings, roles, store, logger)` | 装配工厂，持有 settings / roles / store |
| 4 | `Context(factory, settings.env)` | 构造用例上下文（角色入口 + 变量总线） |
| 5 | `case_cls(ctx).run()` | 按 `suites/regression.yaml` 的 `cases:` 顺序，逐个实例化并运行 |

`--fresh-token` 会在第 2 步后额外执行 `store.clear(env)`，强制后续重新登录。

### 关键点：装配 ≠ 登录

`SessionFactory` 构造时**不发起任何网络请求**。真正的装配发生在第一次
`factory.client(role)` 调用时（`core/session_factory.py:35`）：

```python
strategy = build_strategy(spec.get("auth"))        # roles.yaml → HeaderAuth 实例
client = HttpClient(...).bind_auth(
    strategy,
    token_provider=lambda: self._token_for(role, strategy),   # 懒加载 token
    on_unauthorized=lambda: self._renew(role, strategy),      # 401 自愈
)
```

`HttpClient` 拿到三件套后，**每次发请求前**才去问 `token_provider` 要 token，
所以登录是懒加载的：没人调这个角色的接口，就永远不会登录。

---

## 三、用例生命周期：`BaseCase.run()`

`cases/base_case.py:119` 定义了一条固定轨道：

```
setup_case()  →  test_01_*  →  test_02_*  →  ...  →  teardown_case()
```

两条硬规则：

1. **步骤发现靠方法名字典序**。`step_names()` 用 `inspect.getmembers` 收集所有
   `test_` 开头的函数再 `sorted()`，所以 `test_01_ / test_02_` 的数字前缀就是执行顺序。
2. **单步失败不中断整条流程**。每个 `test_*` 外面套了 `try/except`，异常被记成一条
   `StepResult(ok=False)`，剩下的步骤照跑。这样一次运行就能看到全貌，而不是修一个跑一次。

结果对象：

```
StepResult  单步结果（case, step, ok, detail, duration_ms）
CaseResult  单条用例结果（steps 列表 + ok / passed / failed + 总耗时）
```

`CaseResult.to_dict()` 最终被 runner 汇总进 `reports/report-<env>-<suite>.json`。

---

## 四、OrderFlowCase 逐行拆解

```python
class OrderFlowCase(BaseCase):
    def setup_case(self) -> None:                 # 只构造页面对象，不发请求
        self.product = self.api(ProductApi)       # role = guest
        self.order   = self.api(OrderApi)         # role = merchant
        self.user    = self.api(UserApi)          # role = admin
```

`BaseCase.api()` 带缓存字典，同一个用例里同类型只构造一次；构造过程里
`BaseApi.__init__` 的 `self.client = ctx.client(self.role)` 完成角色→鉴权绑定。

| 步骤 | 做了什么 | 角色 | 断言 |
|---|---|---|---|
| `test_01_browse_products` | `product.list()` 取商品列表，把 `data.items.0.id` 写进 `ctx` | guest | `field("data.items")` |
| `test_02_place_order_by_reference` | `order.create(product_id="${product_id}", quantity=3)`，把 `data.id` 写进 `ctx` | merchant | `field("data.id")` |
| `test_03_read_order_back` | `order.detail("${order_id}")` 回查 | merchant | `field("data.quantity", 3)` |
| `test_04_admin_reads_global_stats` | `order.admin_stats()` → 内部 `as_role("admin")` | admin | `field("data.total_orders")` |
| `test_05_merchant_blocked_on_admin_endpoint` | `order.get("/stats")` 直接以 merchant 访问 | merchant | 期望 403 |

**两个设计要点：**

- `test_02` 的 payload 里写的是字符串 `${product_id}`，替换发生在
  `OrderApi.create()` 的 `self.ctx.interpolate(payload)` 一行——**用例层负责声明"要什么"，
  页面层负责在发请求前把引用解析成真实值**。
- `test_04` 与 `test_05` 是同一组接口、两种角色：`admin_stats()` 走 `as_role("admin")`
  临时切换，`test_05` 用默认的 merchant 打同一个 URL。**同一份代码，因为角色不同，
  得到 200 和 403 两种结果**——这就是权限隔离的验证方式。

---

## 五、一次请求的完整调用链（以 test_02 下单为例）

```
OrderFlowCase.test_02_place_order_by_reference
  └─ OrderApi.create(product_id="${product_id}", quantity=3)
       ├─ self.ctx.interpolate(payload)        # ${product_id} → 1001
       └─ self.post("", json=payload)
            └─ BaseApi.post → self.client.post(...)          # client 已在 __init__ 绑好鉴权
                 └─ HttpClient.request("POST", "/orders", json=...)
                      ├─ token_provider()                     # 只有 needs_token 才问
                      │    └─ SessionFactory.login("merchant")
                      │         ├─ TokenStore.get("dev","merchant")  # 命中 → 直接返回
                      │         └─ 未命中 → _do_login() → POST /auth/login
                      │                       → strategy.extract()  # 按 token_path 取值
                      │                       → TokenStore.set()    # 落盘，供下次复用
                      ├─ strategy.apply(headers, params, cookies, token=...)
                      │       HeaderAuth → headers["X-Merchant-Token"] = token
                      ├─ session.request(...)                 # 真正发出
                      └─ ApiResponse(status_code, text, elapsed_ms, role="merchant")
  └─ self.check("下单（payload 引用 ${product_id}）", resp, 201, expect=field("data.id"))
       └─ StepResult 追加进 self.result.steps
```

### 可靠性行为（都收口在 HttpClient.request）

| 场景 | 处理 |
|---|---|
| 网络异常 | 按 `settings.retries` 重试，退避 `backoff * attempt` 秒 |
| 响应 5xx | 同上重试 |
| 响应 401 且该角色需要 token | 作废缓存 → `_renew()` 重新登录 → **重发一次**（`attempt == 1` 才触发，不会死循环） |
| 响应 401 但该角色 `needs_token == False`（游客） | 直接返回，不重登。游客 401 是权限问题，重登无意义 |

最后一条是实测中修过的 bug：早期版本对游客也触发重登，导致正常的越权拦截被误报成
`AuthError`。

---

## 六、角色与鉴权的调用约定（六条）

1. **角色只在 `config/roles.yaml` 声明一次**，包含三部分：凭据引用（`${VAR}`）、
   `login`（怎么登录）、`auth`（怎么带鉴权）。
2. **页面类只写一行 `role = "merchant"`**。构造函数里 `ctx.client(role)` 返回的客户端
   已经会自己带对应鉴权，**页面方法体里没有任何 token 代码**。
3. **临时换角色用 `as_role("admin")`**，返回新实例，不污染默认角色、不串号。
4. **token 生命周期由框架托管**：缓存命中复用 → 未命中按该角色 `login` 定义登录一次 →
   按 `token_path` 提取 → 落盘；401 自动作废重登并重发一次。
5. **凭据只有两个入口**：`config/secrets.env` 或 CI 同名环境变量，CI 优先；缺变量直接报错。
6. **跨接口传数据用 `${var}`**，URL / payload / params 都能解析，支持 `data.items.0.id`
   这种点号路径。作用域是**本次进程**，不落盘。

### 角色定义示例（roles.yaml）

```yaml
defaults:
  login: { method: POST, path: /auth/login }
  auth:  { token_path: data.access_token, expires_path: data.expires_in }

roles:
  admin:                          # Bearer
    login: { json: { username: "${ADMIN_USERNAME}", password: "${ADMIN_PASSWORD}", role: admin } }
    auth:  { type: bearer, header: Authorization, prefix: "Bearer ", refresh_path: data.refresh_token }

  merchant:                       # 自定义头，且 token 字段位置与 admin 不同
    login: { json: { username: "${MERCHANT_USERNAME}", password: "${MERCHANT_PASSWORD}", role: merchant } }
    auth:  { type: header, header: X-Merchant-Token, token_path: data.token }

  guest:                          # 无鉴权
    login: null
    auth:  { type: none }
```

注意 `merchant` 的 `token_path` 是 `data.token`，`admin` 是 `data.access_token`——
**取 token 的字段位置由角色自己决定**，页面层和用例层完全无感。

---

## 七、扩展时的落点

| 要加什么 | 改哪里 | 成本 |
|---|---|---|
| 新角色 | `config/roles.yaml` 加一段 | 1 个文件 |
| 新鉴权方式 | `core/auth/strategies.py` 写一个 `@register` 类 | 1 个类 |
| 新业务域 | `apis/` 加一个文件，继承 `BaseApi` 并声明 `role` / `prefix` | 1 个文件 |
| 新接口 | 对应页面类里加一个方法 | 1 行 |
| 新用例 | `cases/` 加一个类，写 `test_XX_*` 方法 | 1 个文件 |
| 用例纳入回归 | `suites/regression.yaml` 的 `cases:` 加一行 `模块:类名` | 1 行 |

---

## 八、运行与退出码

```bash
# 终端 A：启动 mock 服务
python tools/mock_server.py

# 终端 B：跑回归套件
python runner/main.py --env dev --suite regression            # 复用缓存 token
python runner/main.py --env dev --suite regression --fresh-token   # 强制重登
python runner/main.py --list-roles                            # 只打印角色与鉴权方式
```

| 退出码 | 含义 |
|---|---|
| 0 | 全部步骤通过 |
| 1 | 有步骤失败 |
| 2 | 框架级错误（配置缺失、角色未定义、登录失败等） |

退出码 0/1/2 可直接用于 CI 判定。

### 实测结论（对 tools/mock_server.py）

| 场景 | 结果 |
|---|---|
| 首次 `--fresh-token` | 2 用例 / 15 步骤全通过，两角色各登录一次 |
| 二次运行（复用缓存） | 15/15 通过，**登录次数 0** |
| 注入伪造 admin token | 触发 401 → 自动重登 → 15/15 通过 |
| 游客访问订单接口 / 商户访问 admin 接口 | 分别 401 / 403 被拦，隔离生效 |
