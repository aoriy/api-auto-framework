"""凭证仓库：按 (环境, 账号) 索引，进程内复用 + 可选磁盘复用。

设计要点：
  - 索引维度是**账号**，不是角色。同一角色可能有多个账号（采购员 A / 采购员 B），
    按角色索引会让它们互相覆盖。
  - 一个环境下一个账号只存一份凭证快照，避免每个用例都登录一次。
  - 凭证有两种形态，统一放在同一个 Token 对象里：
      * token 类（bearer / header / query / signature）→ value 有值，cookies 为空
      * 会话类（cookie）→ cookies 有值（整份 CookieJar 快照），value 为空
  - 落盘只是「复用」不是「可信来源」，文件删掉也能跑（会重新登录）。
  - **安全提示**：cookies 落盘等于明文会话落盘。runtime/ 必须进 .gitignore，
    生产环境建议把 persist 关掉（env yaml 里设 persist_credentials: false）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Token:
    """一个账号的凭证快照。token 类用 value，会话类用 cookies。"""

    account: str
    env: str
    value: str = ""                                  # token 类凭证
    cookies: dict = field(default_factory=dict)      # 会话类凭证（CookieJar 快照）
    expires_at: float = 0.0                          # epoch 秒；0 表示没有有效期，不过期
    refresh_token: str | None = None
    obtained_at: float = field(default_factory=time.time)

    @property
    def role(self) -> str:
        """兼容旧字段名：现在承载的是账号名。"""
        return self.account

    @property
    def kind(self) -> str:
        return "cookie" if self.cookies else "token"

    def is_valid(self, leeway: int = 30) -> bool:
        """留 30 秒安全边际，避免边界上刚好过期。"""
        if not self.expires_at:
            return True
        return time.time() < (self.expires_at - leeway)

    @property
    def remaining(self) -> float:
        if not self.expires_at:
            return float("inf")
        return max(0.0, self.expires_at - time.time())

    def describe(self) -> str:
        left = self.remaining
        text = "无期限" if left == float("inf") else f"{left:.0f}s 后过期"
        if self.cookies:
            names = ",".join(sorted(self.cookies))[:28]
            return f"{self.account}@{self.env} 会话[{names}] ({text})"
        return f"{self.account}@{self.env} {self.value[:8]}... ({text})"


class TokenStore:
    def __init__(self, cache_file: Path | str | None = None, persist: bool = True):
        self.cache_file = Path(cache_file) if cache_file else None
        self.persist = persist
        self._memory: dict[str, Token] = {}
        if self.cache_file and self.persist and self.cache_file.exists():
            self._load_from_disk()

    @staticmethod
    def _key(env: str, account: str) -> str:
        return f"{env}::{account}"

    def get(self, env: str, account: str) -> Token | None:
        """拿到有效凭证；不存在或已过期返回 None。"""
        token = self._memory.get(self._key(env, account))
        if token and token.is_valid():
            return token
        return None

    def peek(self, env: str, account: str) -> Token | None:
        """不管过期与否都拿（给报告展示用）。"""
        return self._memory.get(self._key(env, account))

    def set(self, token: Token) -> Token:
        self._memory[self._key(token.env, token.account)] = token
        self._persist()
        return token

    def invalidate(self, env: str, account: str) -> None:
        self._memory.pop(self._key(env, account), None)
        self._persist()

    def clear(self, env: str | None = None) -> None:
        if env is None:
            self._memory.clear()
        else:
            for key in [k for k in self._memory if k.startswith(f"{env}::")]:
                self._memory.pop(key, None)
        self._persist()

    def all(self) -> list[Token]:
        return list(self._memory.values())

    # ---------------- 磁盘 ----------------
    def _load_from_disk(self) -> None:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("tokens", []):
            # 兼容旧版本写下的 role 字段
            if "role" in item and "account" not in item:
                item["account"] = item.pop("role")
            try:
                token = Token(**item)
            except TypeError:
                continue
            self._memory[self._key(token.env, token.account)] = token

    def _persist(self) -> None:
        if not self.cache_file or not self.persist:
            return
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tokens": [asdict(t) for t in self._memory.values()]}
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
