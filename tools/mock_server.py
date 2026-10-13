"""纯标准库 mock API 服务：用来验证框架本身能不能跑通。

三个角色、三种鉴权形态，而且故意做得不整齐，用来证明框架能按角色自动适配：
  admin    -> Authorization: Bearer <token>   ，token 落在 data.access_token
  merchant -> X-Merchant-Token: <token>       ，token 落在 data.token（字段名不同！）
  guest    -> 不带任何鉴权，只能读 /products

账号口令**不写在源码里**，启动时从 config/secrets.env 读取
（真实环境变量优先）。变量名：ADMIN_/MERCHANT_/GUEST_ + USERNAME/PASSWORD。

启动：python tools/mock_server.py    默认监听 127.0.0.1:18888
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST, PORT = "127.0.0.1", 18888
TOKEN_TTL = 3600

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / "config" / "secrets.env"


def _load_dotenv(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的凭据文件（真实环境变量优先，便于 CI 覆盖）。"""
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


# 角色 -> (用户名变量名, 口令变量名)
ACCOUNT_CREDENTIALS = {
    "admin": ("ADMIN_USERNAME", "ADMIN_PASSWORD"),
    "merchant": ("MERCHANT_USERNAME", "MERCHANT_PASSWORD"),
    "guest": ("GUEST_USERNAME", "GUEST_PASSWORD"),
}


def load_accounts() -> dict[tuple[str, str], str]:
    """构造 (用户名, 口令) -> 角色 的登录表。

    ★ 口令**只从 config/secrets.env（或真实环境变量）读取**，源码里不留明文。
      与 config/settings.py 使用同一套解析约定：真实环境变量优先于凭据文件。
    """
    env = {**_load_dotenv(SECRETS_FILE), **os.environ}
    accounts: dict[tuple[str, str], str] = {}
    missing: list[str] = []
    for role, (user_key, pwd_key) in ACCOUNT_CREDENTIALS.items():
        username, password = env.get(user_key, ""), env.get(pwd_key, "")
        if not username or not password:
            missing.append(f"{user_key} / {pwd_key}")
            continue
        accounts[(username, password)] = role
    if missing:
        raise SystemExit(
            "凭据缺失：" + "、".join(missing) + " 未在 config/secrets.env 中配置。\n"
            f"凭据文件：{SECRETS_FILE}\n"
            "请参照 config/secrets.env.example 补齐后重启本 mock 服务。"
        )
    return accounts


ACCOUNTS = load_accounts()

LOCK = threading.Lock()
TOKENS: dict[str, dict] = {}
ORDERS: dict[str, dict] = {}
COUNTER = {"order": 0}

PRODUCTS = [
    {"id": 1001, "name": "Mechanical Keyboard", "price": 699.0, "stock": 42},
    {"id": 1002, "name": "Noise Cancelling Headphones", "price": 1299.0, "stock": 15},
    {"id": 1003, "name": "27-inch 4K Monitor", "price": 2499.0, "stock": 8},
]

USERS = {
    "admin": {"id": 1, "name": "admin", "role": "admin", "email": "admin@example.com"},
    "merchant": {"id": 2, "name": "merchant", "role": "merchant", "email": "merchant@example.com"},
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockAPI/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础设施 ----------------
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, *, reset: bool = False) -> dict:
        """读走并解析请求体。

        ★ 必须在鉴权判断之前调用（reset=True）：HTTP/1.1 keep-alive 下，鉴权失败
        若直接返回而没读走请求体，残留字节会被当成下一个请求的请求行 → 后续 400。
        ★ keep-alive 连接上 handler 实例会复用，缓存必须每请求重置。
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

    def _role(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            info = TOKENS.get(auth[7:].strip())
            if info:
                return info["role"]
        header_token = (self.headers.get("X-Merchant-Token") or "").strip()
        if header_token:
            info = TOKENS.get(header_token)
            if info:
                return info["role"]
        return None

    def _require(self, *allowed: str) -> str | None:
        role = self._role()
        if role is None:
            self._json(401, {"code": 401, "message": "missing or invalid credential"})
            return None
        if allowed and role not in allowed:
            self._json(403, {"code": 403, "message": f"role '{role}' is not allowed here"})
            return None
        return role

    def log_message(self, fmt, *args) -> None:      # noqa: A002
        print(f"[mock] {args[0]} -> {args[1]}", flush=True)

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            return self._json(200, {"status": "ok"})

        if path == "/products":
            return self._json(200, {"code": 0,
                                    "data": {"items": PRODUCTS, "total": len(PRODUCTS)}})

        if path.startswith("/products/"):
            pid = path.split("/")[2]
            item = next((p for p in PRODUCTS if str(p["id"]) == str(pid)), None)
            if item is None:
                return self._json(404, {"code": 404, "message": "product not found"})
            return self._json(200, {"code": 0, "data": item})

        if path == "/users/me":
            role = self._require()
            if role is None:
                return
            user = USERS.get(role)
            if user is None:
                return self._json(404, {"code": 404, "message": "no profile for this role"})
            return self._json(200, {"code": 0, "data": user})

        if path == "/users":
            if self._require("admin") is None:
                return
            return self._json(200, {"code": 0, "data": {"items": list(USERS.values())}})

        if path == "/orders/stats":                    # 必须排在 /orders/ 之前
            if self._require("admin") is None:
                return
            with LOCK:
                items = list(ORDERS.values())
            total_amount = sum(o["quantity"] * o["unit_price"] for o in items)
            return self._json(200, {"code": 0,
                                    "data": {"total_orders": len(items),
                                             "total_amount": total_amount}})

        if path == "/orders":
            role = self._require("admin", "merchant")
            if role is None:
                return
            with LOCK:
                items = (list(ORDERS.values()) if role == "admin"
                         else [o for o in ORDERS.values() if o["role"] == role])
            return self._json(200, {"code": 0, "data": {"items": items, "total": len(items)}})

        if path.startswith("/orders/"):
            if self._require("admin", "merchant") is None:
                return
            oid = path.split("/")[2]
            with LOCK:
                order = ORDERS.get(oid)
            if order is None:
                return self._json(404, {"code": 404, "message": "order not found"})
            return self._json(200, {"code": 0, "data": order})

        return self._json(404, {"code": 404, "message": f"no route for GET {path}"})

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        self._read_body(reset=True)     # ★ 每请求重置 + 先读走请求体
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/auth/login":
            body = self._read_body()
            key = (str(body.get("username", "")), str(body.get("password", "")))
            with LOCK:
                role = ACCOUNTS.get(key)
            if role is None:
                return self._json(401, {"code": 401, "message": "bad username or password"})
            token = f"tok-{role}-{int(time.time() * 1000)}"
            with LOCK:
                TOKENS[token] = {"role": role, "exp": time.time() + TOKEN_TTL}
            if role == "merchant":
                data = {"token": token, "expires_in": TOKEN_TTL, "role": role}
            else:
                data = {"access_token": token, "expires_in": TOKEN_TTL,
                        "refresh_token": f"refresh-{role}", "role": role}
            return self._json(200, {"code": 0, "message": "ok",
                                    "data": data, "user": USERS.get(role, {"role": role})})

        if path == "/orders":
            role = self._require("admin", "merchant")
            if role is None:
                return
            body = self._read_body()
            product_id = body.get("product_id")
            product = next((p for p in PRODUCTS if str(p["id"]) == str(product_id)), None)
            if product is None:
                return self._json(400, {"code": 400,
                                        "message": f"unknown product_id={product_id}"})
            try:
                quantity = int(body.get("quantity", 1))
            except (TypeError, ValueError):
                return self._json(400, {"code": 400, "message": "quantity must be an integer"})
            with LOCK:
                COUNTER["order"] += 1
                order_id = str(COUNTER["order"])
                order = {
                    "id": order_id,
                    "role": role,
                    "product_id": product["id"],
                    "product_name": product["name"],
                    "quantity": quantity,
                    "unit_price": product["price"],
                    "status": "created",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                ORDERS[order_id] = order
            return self._json(201, {"code": 0, "message": "created", "data": order})

        if path.startswith("/orders/") and path.endswith("/cancel"):
            role = self._require("admin", "merchant")
            if role is None:
                return
            oid = path.split("/")[2]
            with LOCK:
                order = ORDERS.get(oid)
                if order is None:
                    return self._json(404, {"code": 404, "message": "order not found"})
                if role != "admin" and order["role"] != role:
                    return self._json(403, {"code": 403, "message": "not your order"})
                order["status"] = "cancelled"
            return self._json(200, {"code": 0, "data": order})

        return self._json(404, {"code": 404, "message": f"no route for POST {path}"})

    # ---------------- PUT ----------------
    def do_PUT(self) -> None:
        self._read_body(reset=True)     # ★ 同上
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/users/"):
            if self._require("admin") is None:
                return
            body = self._read_body()
            return self._json(200, {"code": 0, "data": {"id": path.split("/")[2], **body}})
        return self._json(404, {"code": 404, "message": f"no route for PUT {path}"})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[mock] listening on http://{HOST}:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
