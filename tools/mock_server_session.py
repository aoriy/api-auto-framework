"""基于**会话 Cookie**的 mock API 服务：用来验证多账号切换方案。

和 tools/mock_server.py（Bearer / 自定义头）的区别，也是这个服务存在的意义：

  1. 身份不在请求头里，而在 `SESSION` Cookie 里 —— 想换账号**必须换 CookieJar**，
     没法靠改一个请求头蒙混过去。
  2. CSRF token 与 session 绑定：写操作必须带 `X-CSRFToken`，且必须等于
     **当前这个 session** 的 CSRF 值。拿 A 的 CSRF 配 B 的 SESSION → 403。
     这条正是「切换账号时最容易被忽略的坑」。
  3. 多个同角色账号（buyer_a / buyer_b），数据互相不可见 —— 用来证明会话隔离。

★ 账号从哪来
    账号注册表**全部来自 `config/accounts.session.yaml`**（口令取自 config/secrets.env），
    服务端不再硬编码任何账号或口令。也就是说：

        「在系统里加一个账号」= 只改 config/accounts.session.yaml（+ secrets.env 补口令）

    这样账号表就是唯一事实来源，服务端与测试端不可能再各说各话。
    规则：只有配置了 login.json.username / password 的条目才会成为可登录账号；
    login: null（如 guest_web）与 OAuth 客户端条目（如 partner_bot）不进注册表。

★ 变更流程的角色分工（本次新增）
    creator        流程创建人  —— 发起变更流程
    sre_expert     SRE 专家    —— 评审变更
    stg_maintainer 维护 STG    —— 部署变更到 STG（必须先通过 SRE 评审）

启动：python tools/mock_server_session.py    默认监听 127.0.0.1:18889
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

HOST, PORT = "127.0.0.1", 18889

SESSION_COOKIE = "SESSION"
CSRF_COOKIE = "csrftoken"
CSRF_HEADER = "X-CSRFToken"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_FILE = ROOT / "config" / "accounts.session.yaml"
SECRETS_FILE = ROOT / "config" / "secrets.env"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ======================================================================
# 账号注册表：从账号表文件载入（唯一事实来源）
# ======================================================================
def _load_dotenv(path: Path) -> dict:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _expand(node, env: dict):
    """把 ${VAR} 展开成真实值；变量缺失时**保留原样**（便于识别未填充的配置）。"""
    if isinstance(node, dict):
        return {k: _expand(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v, env) for v in node]
    if isinstance(node, str):
        return _ENV_REF.sub(lambda m: env.get(m.group(1), m.group(0)), node)
    return node


def load_registry(path: Path = REGISTRY_FILE, secrets_file: Path = SECRETS_FILE):
    """载入账号注册表。

    返回 (账号字典, 开放平台客户端字典, 被跳过的条目说明)。
    跳过 = 该条目不是可登录账号（无 login / OAuth 客户端 / 口令变量未填充）。
    """
    if not path.exists():
        raise SystemExit(
            f"找不到账号表 {path}\n"
            f"服务端账号完全来自该文件，请先创建它（可参考 config/accounts.session.yaml）。"
        )
    # 真实环境变量优先，便于 CI 覆盖本地凭据文件
    env = {**_load_dotenv(secrets_file), **os.environ}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = (raw.get("defaults") or {}).get("login") or {}

    accounts: dict[str, dict] = {}
    partners: dict[str, str] = {}
    skipped: list[str] = []

    for name, spec in (raw.get("accounts") or {}).items():
        spec = spec or {}
        login = {**defaults, **(spec.get("login") or {})}
        if not login:
            skipped.append(f"{name}（无 login，非登录账号）")
            continue
        payload = _expand(login.get("json") or {}, env)
        path_fragment = str(login.get("path") or "")

        # --- 开放平台客户端：走 /oauth/token，用 client_id / client_secret ---
        if path_fragment.endswith("/oauth/token"):
            cid, csec = payload.get("client_id"), payload.get("client_secret")
            if cid and csec and "${" not in str(cid):
                partners[str(cid)] = str(csec)
            else:
                skipped.append(f"{name}（OAuth 客户端凭证未填充）")
            continue

        # --- 普通账号：username / password ---
        username, password = payload.get("username"), payload.get("password")
        if not username or password in (None, ""):
            skipped.append(f"{name}（缺少 username / password）")
            continue
        if "${" in str(password):
            skipped.append(f"{name}（口令变量未填充：{password}）")
            continue

        accounts[str(username)] = {
            "password": str(password),
            "role": spec.get("role") or "guest",
            "name": spec.get("description") or name,
            "id": int(spec.get("id") or (3000 + len(accounts) + 1)),
        }

    return accounts, partners, skipped


ACCOUNTS, PARTNER_CLIENTS, SKIPPED = load_registry()

CATALOG = [
    {"id": "SKU-001", "name": "Mechanical Keyboard", "price": 699.0},
    {"id": "SKU-002", "name": "Noise Cancelling Headphones", "price": 1299.0},
    {"id": "SKU-003", "name": "27-inch 4K Monitor", "price": 2499.0},
]

BEARER_TOKENS: dict[str, str] = {}      # token -> client_id

LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}      # sid -> {account, role, csrf, created}
ORDERS: dict[str, dict] = {}        # oid -> order
CHANGES: dict[str, dict] = {}       # cid -> 变更流程
CASES: dict[str, dict] = {}         # case_id -> 问题处理单（8 个页面阶段）
COUNTER = {"order": 0, "change": 0, "case": 0}

# ======================================================================
# 8 个页面阶段的状态机（本 mock 用来验证「整体用例」能把页面串起来）
# ----------------------------------------------------------------------
# action           页面名              前置状态                推进到
# 每个阶段只接受「上一步刚完成」的状态，越阶一律 409 —— 这样「顺序约束」
# 才是真的被校验，而不是靠用例自己按顺序调用装样子。
# ======================================================================
CASE_ACTIONS: dict[str, tuple[str, str, str]] = {
    "expert-review":         ("专家评审页面",     "applied",                  "expert_reviewed"),
    "fae-summary":           ("FAE 汇总页面",     "expert_reviewed",          "fae_summarized"),
    "ipq-review":            ("IPQ 审核页面",     "fae_summarized",           "ipq_approved"),
    "fae-lead-review":       ("FAE 组长审核页面", "ipq_approved",             "lead_approved"),
    "investigate":           ("排查中页面",       "lead_approved",            "investigating"),
    "investigation-summary": ("排查汇总页面",     "investigating",            "investigation_summarized"),
    "complete":              ("完成页面",         "investigation_summarized", "completed"),
}

PAGE_ORDER = ["申请页面"] + [spec[0] for spec in CASE_ACTIONS.values()]


def _case_view(case: dict) -> dict:
    """单据对外视图（阶段列表一并返回，便于用例核对 8 个页面都真实生效）。"""
    return {
        "id": case["id"],
        "title": case["title"],
        "product": case["product"],
        "priority": case["priority"],
        "creator": case["creator"],
        "status": case["status"],
        "stage_count": len(case["stages"]),
        "stages": [dict(s) for s in case["stages"]],
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SessionMock/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础设施 ----------------
    def _json(self, status: int, payload: dict, cookies: list[str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, *, reset: bool = False) -> dict:
        """读走并解析请求体。

        ★ 必须在任何鉴权判断之前调用一次（reset=True）。HTTP/1.1 是 keep-alive，
        鉴权失败若直接返回而没读走请求体，残留字节会被当成下一个请求的请求行，
        导致同一连接上的后续请求全部 400。

        ★ 同时注意：keep-alive 连接上 BaseHTTPRequestHandler 会**复用同一个实例**
        处理多个请求，所以缓存必须每请求重置，否则第二个请求会读到第一个的 body。
        """
        if reset or getattr(self, "_body_cache", None) is None:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                self._body_cache = {}
            else:
                try:
                    self._body_cache = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    self._body_cache = {}
        return self._body_cache

    def _cookies_in(self) -> dict:
        raw = self.headers.get("Cookie") or ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return {}
        return {k: m.value for k, m in jar.items()}

    def _session(self):
        """返回 (sid, session_dict)，未登录返回 (None, None)。"""
        sid = self._cookies_in().get(SESSION_COOKIE)
        if not sid:
            return None, None
        with LOCK:
            sess = SESSIONS.get(sid)
        return (sid, sess) if sess else (None, None)

    def _require(self, *roles: str):
        """鉴权 + 授权 + CSRF。返回 session dict；失败时已经写好响应并返回 None。"""
        sid, sess = self._session()
        if sess is None:
            self._json(401, {"code": 401, "message": "missing or invalid session cookie"})
            return None
        if roles and sess["role"] not in roles:
            self._json(403, {"code": 403,
                             "message": f"account '{sess['account']}' (role={sess['role']}) "
                                        f"is not allowed here",
                             "required_roles": list(roles)})
            return None
        # CSRF：写操作必须带与当前 session 绑定的 token
        if self.command.upper() not in _SAFE_METHODS:
            sent = (self.headers.get(CSRF_HEADER) or "").strip()
            if not sent or sent != sess["csrf"]:
                self._json(403, {"code": 403,
                                 "message": "csrf token invalid for this session",
                                 "hint": f"需要与本 session 绑定的 {CSRF_HEADER}"})
                return None
        return sess

    def log_message(self, fmt, *args) -> None:      # noqa: A002
        print(f"[session-mock] {args[0]} -> {args[1]}", flush=True)

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            return self._json(200, {"status": "ok"})

        if path == "/registry/accounts":
            # 只暴露账号与角色，绝不返回口令 —— 用来核对「账号是否已建立、角色是什么」
            return self._json(200, {"code": 0, "data": {
                "accounts": [{"account": a, "role": i["role"], "name": i["name"]}
                             for a, i in sorted(ACCOUNTS.items())],
                "total": len(ACCOUNTS)}})

        if path == "/catalog":
            return self._json(200, {"code": 0, "data": {"items": CATALOG}})

        if path == "/debug/whoami":
            # 用来证明「每个账号的 SESSION 与 CSRF 都不同」
            sid, sess = self._session()
            if sess is None:
                return self._json(401, {"code": 401, "message": "not logged in"})
            return self._json(200, {"code": 0, "data": {
                "account": sess["account"],
                "role": sess["role"],
                "session_prefix": sid[:12],
                "csrf_prefix": sess["csrf"][:12],
                "cookies": sorted(self._cookies_in()),
            }})

        if path == "/partner/ping":
            # Authorization 可选入口：不依赖 Cookie，纯请求头鉴权
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                return self._json(401, {"code": 401, "message": "missing bearer token"})
            client_id = BEARER_TOKENS.get(auth[7:].strip())
            if client_id is None:
                return self._json(401, {"code": 401, "message": "invalid bearer token"})
            return self._json(200, {"code": 0, "data": {
                "client": client_id, "auth": "bearer", "cookies_used": bool(self._cookies_in())}})

        if path == "/me":
            sess = self._require()
            if sess is None:
                return
            info = ACCOUNTS[sess["account"]]
            return self._json(200, {"code": 0, "data": {
                "id": info["id"], "account": sess["account"],
                "role": info["role"], "name": info["name"]}})

        if path == "/cart/orders":
            sess = self._require()
            if sess is None:
                return
            with LOCK:
                # 管理员看全量，采购员只看自己的 —— 同角色不同账号之间也互不可见
                items = ([o for o in ORDERS.values() if o["buyer"] == sess["account"]]
                         if sess["role"] == "buyer" else list(ORDERS.values()))
            return self._json(200, {"code": 0, "data": {"items": items, "total": len(items)}})

        if path == "/admin/overview":
            sess = self._require("admin")
            if sess is None:
                return
            with LOCK:
                orders = list(ORDERS.values())
            return self._json(200, {"code": 0, "data": {
                "total_orders": len(orders),
                "approved": sum(1 for o in orders if o["status"] == "approved"),
                "by_buyer": {b: sum(1 for o in orders if o["buyer"] == b)
                             for b in sorted({o["buyer"] for o in orders})},
            }})

        if path.startswith("/cart/orders/"):
            sess = self._require()
            if sess is None:
                return
            oid = path.split("/")[3]
            with LOCK:
                order = ORDERS.get(oid)
            if order is None:
                return self._json(404, {"code": 404, "message": "order not found"})
            # 跨账号读取：本人或管理员才行
            if sess["role"] != "admin" and order["buyer"] != sess["account"]:
                return self._json(403, {"code": 403,
                                        "message": "this order belongs to another account"})
            return self._json(200, {"code": 0, "data": order})

        # ---------------- 变更流程 ----------------
        if path == "/flow/changes":
            sess = self._require()
            if sess is None:
                return
            with LOCK:
                items = sorted(CHANGES.values(), key=lambda c: c["id"])
            # 创建人只看自己发起的；评审方/维护方需要看到全部
            if sess["role"] == "creator":
                items = [c for c in items if c["creator"] == sess["account"]]
            return self._json(200, {"code": 0, "data": {"items": items, "total": len(items)}})

        if path.startswith("/flow/changes/"):
            sess = self._require()
            if sess is None:
                return
            cid = path.split("/")[3]
            with LOCK:
                change = CHANGES.get(cid)
            if change is None:
                return self._json(404, {"code": 404, "message": "change not found"})
            if sess["role"] == "creator" and change["creator"] != sess["account"]:
                return self._json(403, {"code": 403,
                                        "message": "this change belongs to another creator"})
            return self._json(200, {"code": 0, "data": change})

        # ---------------- 单据（8 个页面） ----------------
        if path == "/flow/cases":
            # 列表查询：支持 ?status= 过滤。同一个 path 上 GET 列表 / POST 新建，互不冲突
            sess = self._require()
            if sess is None:
                return
            qs = parse_qs(urlparse(self.path).query)
            wanted = (qs.get("status") or [None])[0]
            with LOCK:
                items = [{"id": c["id"], "title": c["title"], "status": c["status"],
                          "creator": c["creator"], "stage_count": len(c["stages"])}
                         for c in CASES.values()
                         if wanted is None or c["status"] == wanted]
            return self._json(200, {"code": 0, "data": {"items": items, "total": len(items)}})

        if path.startswith("/flow/cases/"):
            sess = self._require()
            if sess is None:
                return
            cid = path.split("/")[3]
            with LOCK:
                case = CASES.get(cid)
                snapshot = _case_view(case) if case else None
            if snapshot is None:
                return self._json(404, {"code": 404, "message": "case not found"})
            return self._json(200, {"code": 0, "data": snapshot})

        return self._json(404, {"code": 404, "message": f"no route for GET {path}"})

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        self._read_body(reset=True)     # ★ 每请求重置 + 先读走请求体
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/auth/login":
            body = self._read_body()
            account = str(body.get("username", ""))
            password = str(body.get("password", ""))
            info = ACCOUNTS.get(account)
            if info is None or info["password"] != password:
                return self._json(401, {"code": 401, "message": "bad username or password"})

            sid = "sess-" + secrets.token_hex(12)
            csrf = "csrf-" + secrets.token_hex(12)
            with LOCK:
                # 登录即换新会话：旧 session 立即失效（防会话固定）
                SESSIONS[sid] = {"account": account, "role": info["role"],
                                 "csrf": csrf, "created": time.time()}
            cookies = [
                f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax",
                f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax",
            ]
            return self._json(200, {"code": 0, "message": "ok", "data": {
                "account": account, "role": info["role"], "name": info["name"],
                "csrf_token": csrf}}, cookies=cookies)

        if path == "/oauth/token":
            # Authorization 可选入口：给开放平台客户端签发令牌
            body = self._read_body()
            client_id = str(body.get("client_id", ""))
            secret = str(body.get("client_secret", ""))
            if PARTNER_CLIENTS.get(client_id) != secret:
                return self._json(401, {"code": 401, "message": "bad client credentials"})
            token = "pt-" + secrets.token_hex(16)
            with LOCK:
                BEARER_TOKENS[token] = client_id
            return self._json(200, {"code": 0, "data": {
                "access_token": token, "token_type": "Bearer",
                "expires_in": 3600, "client_id": client_id}})

        if path == "/auth/logout":
            sid, sess = self._session()
            if sess is not None:
                with LOCK:
                    SESSIONS.pop(sid, None)
            cookies = [f"{SESSION_COOKIE}=; Path=/; Max-Age=0",
                       f"{CSRF_COOKIE}=; Path=/; Max-Age=0"]
            return self._json(200, {"code": 0, "message": "logged out"}, cookies=cookies)

        if path == "/cart/orders":
            sess = self._require("buyer")        # 下单只能是采购员
            if sess is None:
                return
            body = self._read_body()
            sku = next((p for p in CATALOG if p["id"] == body.get("product_id")), None)
            if sku is None:
                return self._json(400, {"code": 400,
                                        "message": f"unknown product_id={body.get('product_id')}"})
            try:
                quantity = int(body.get("quantity", 1))
            except (TypeError, ValueError):
                return self._json(400, {"code": 400, "message": "quantity must be an integer"})
            with LOCK:
                COUNTER["order"] += 1
                oid = f"O{COUNTER['order']:04d}"
                order = {
                    "id": oid, "buyer": sess["account"], "buyer_role": sess["role"],
                    "product_id": sku["id"], "product_name": sku["name"],
                    "quantity": quantity, "unit_price": sku["price"],
                    "amount": round(quantity * sku["price"], 2),
                    "status": "created",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                ORDERS[oid] = order
            return self._json(201, {"code": 0, "message": "created", "data": order})

        if path.startswith("/cart/orders/") and path.endswith("/approve"):
            sess = self._require("admin")        # 审批只能是管理员
            if sess is None:
                return
            oid = path.split("/")[3]
            with LOCK:
                order = ORDERS.get(oid)
                if order is None:
                    return self._json(404, {"code": 404, "message": "order not found"})
                order["status"] = "approved"
                order["approved_by"] = sess["account"]
                order["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return self._json(200, {"code": 0, "data": order})

        # ---------------- 变更流程 ----------------
        if path == "/flow/changes":
            sess = self._require("creator")      # 发起流程只能是创建人
            if sess is None:
                return
            body = self._read_body()
            title = str(body.get("title") or "").strip()
            if not title:
                return self._json(400, {"code": 400, "message": "title is required"})
            with LOCK:
                COUNTER["change"] += 1
                cid = f"CH{COUNTER['change']:04d}"
                change = {
                    "id": cid, "title": title,
                    "service": str(body.get("service") or "gateway"),
                    "creator": sess["account"], "status": "created",
                    "reviewed_by": None, "deployed_by": None,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                CHANGES[cid] = change
            return self._json(201, {"code": 0, "message": "created", "data": change})

        if path.startswith("/flow/changes/") and path.endswith("/sre-review"):
            sess = self._require("sre_expert")   # 评审只能是 SRE 专家
            if sess is None:
                return
            cid = path.split("/")[3]
            with LOCK:
                change = CHANGES.get(cid)
                if change is None:
                    return self._json(404, {"code": 404, "message": "change not found"})
                change["status"] = "sre_approved"
                change["reviewed_by"] = sess["account"]
                change["reviewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                snapshot = dict(change)
            return self._json(200, {"code": 0, "data": snapshot})

        if path.startswith("/flow/changes/") and path.endswith("/stg-deploy"):
            sess = self._require("stg_maintainer")   # 部署只能是 STG 维护
            if sess is None:
                return
            cid = path.split("/")[3]
            with LOCK:
                change = CHANGES.get(cid)
                if change is None:
                    return self._json(404, {"code": 404, "message": "change not found"})
                current = change["status"]
                if current != "sre_approved":
                    allowed, snapshot = False, None
                else:
                    allowed = True
                    change["status"] = "deployed_stg"
                    change["deployed_by"] = sess["account"]
                    change["deployed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    snapshot = dict(change)
            if not allowed:
                return self._json(409, {"code": 409,
                                        "message": f"change {cid} 必须先通过 SRE 评审"
                                                   f"（当前状态 '{current}'）"})
            return self._json(200, {"code": 0, "data": snapshot})

        # ---------------- 单据：8 个页面阶段 ----------------
        if path == "/flow/cases":
            sess = self._require()               # 提交申请：只要求已登录
            if sess is None:
                return
            body = self._read_body()
            title = str(body.get("title") or "").strip()
            if not title:
                return self._json(400, {"code": 400, "message": "title is required"})
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            with LOCK:
                COUNTER["case"] += 1
                cid = f"CASE{COUNTER['case']:04d}"
                case = {
                    "id": cid, "title": title,
                    "product": str(body.get("product") or ""),
                    "priority": str(body.get("priority") or "P3"),
                    "creator": sess["account"], "status": "applied",
                    "created_at": now, "updated_at": now,
                    "stages": [{"page": "申请页面", "status": "applied",
                                "operator": sess["account"], "at": now,
                                "detail": title}],
                }
                CASES[cid] = case
                snapshot = _case_view(case)
            return self._json(201, {"code": 0, "message": "created", "data": snapshot})

        if path.startswith("/flow/cases/"):
            parts = [p for p in path.split("/") if p]        # flow / cases / <id> / <action>
            if len(parts) != 4:
                return self._json(404, {"code": 404,
                                        "message": f"no route for POST {path}"})
            cid, action = parts[2], parts[3]
            spec = CASE_ACTIONS.get(action)
            if spec is None:
                return self._json(404, {"code": 404,
                                        "message": f"unknown action '{action}'"})
            page_name, required_status, next_status = spec
            sess = self._require()               # 已登录即可；权限按需在此收紧
            if sess is None:
                return
            body = self._read_body()
            with LOCK:
                case = CASES.get(cid)
                if case is None:
                    return self._json(404, {"code": 404, "message": "case not found"})
                current = case["status"]
                # ★ 顺序约束：每个页面只接受「上一步刚完成」的状态，越阶 409
                if current != required_status:
                    return self._json(409, {
                        "code": 409,
                        "message": f"单据 {cid} 当前状态 '{current}'，不能直接进入"
                                   f"「{page_name}」（需要先处于 '{required_status}'）",
                        "current_status": current,
                        "required_status": required_status,
                    })
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                case["status"] = next_status
                case["updated_at"] = now
                case["stages"].append({
                    "page": page_name, "status": next_status,
                    "operator": sess["account"], "at": now,
                    "detail": str(body.get("comment") or body.get("summary")
                                  or body.get("conclusion") or body.get("note")
                                  or body.get("action") or body.get("remark")
                                  or body.get("root_cause") or ""),
                })
                snapshot = _case_view(case)
            return self._json(200, {"code": 0, "data": snapshot})

        return self._json(404, {"code": 404, "message": f"no route for POST {path}"})

    # ---------------- PUT ----------------
    def do_PUT(self) -> None:
        self._read_body(reset=True)     # ★ 同上
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/admin/session/expire":
            # 测试用：让指定账号的会话立即失效，用来验证 401 自动重登
            sess = self._require("admin")
            if sess is None:
                return
            body = self._read_body()
            target = str(body.get("account", ""))
            with LOCK:
                doomed = [sid for sid, s in SESSIONS.items() if s["account"] == target]
                for sid in doomed:
                    SESSIONS.pop(sid, None)
            return self._json(200, {"code": 0, "data": {"expired_sessions": len(doomed),
                                                        "account": target}})
        return self._json(404, {"code": 404, "message": f"no route for PUT {path}"})


def main() -> None:
    by_role: dict[str, list[str]] = {}
    for account, info in sorted(ACCOUNTS.items()):
        by_role.setdefault(info["role"], []).append(account)

    print(f"[session-mock] 账号来自 {REGISTRY_FILE.name}，共 {len(ACCOUNTS)} 个：", flush=True)
    for role in sorted(by_role):
        print(f"[session-mock]   {role:<15} {' , '.join(by_role[role])}", flush=True)
    if SKIPPED:
        print(f"[session-mock] 已跳过 {len(SKIPPED)} 个条目：", flush=True)
        for item in SKIPPED:
            print(f"[session-mock]   - {item}", flush=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[session-mock] listening on http://{HOST}:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
