"""配置装载：环境配置（环境文件）+ 凭据（独立文件）分开管理。

分工：
  config/env/<env>.yaml        —— 非敏感配置：base_url、超时、重试、账号表路径、日志级别
  config/accounts*.yaml        —— 账号表：怎么登录、怎么带鉴权（只写变量名，不写明文）
  config/secrets.env           —— 明文凭据（不进版本库；CI 里用真环境变量覆盖）

关于「账号」与「角色」：
  * 账号（account）是索引维度，也是登录单位 —— 一个账号 = 一套凭据 + 一次会话。
  * 角色（role）是账号的一个属性，描述业务权限类别（buyer / admin / …），
    同角色可以有多个账号（采购员 A、采购员 B），它们必须各自独立登录、互不串号。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.exceptions import ConfigError

ROOT = Path(__file__).resolve().parent.parent

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_secrets(path: Path) -> int:
    """把 key=value 凭据文件读进 os.environ。

    已经存在的真实环境变量优先（setdefault），所以 CI Secret 不会被本地文件覆盖。
    返回加载的条目数。
    """
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        count += 1
    return count


@dataclass(frozen=True)
class Settings:
    env: str
    base_url: str
    timeout: float
    retries: int
    backoff: float
    verify_ssl: bool
    log_level: str
    accounts_file: Path
    credential_cache: Path
    persist_credentials: bool
    #: 页面 URL 的公共前缀。页面类未声明 prefix 时统一取它；
    #: "" 表示无前缀（接口路径本身就是完整路径）。
    api_prefix: str = ""

    @property
    def roles_file(self) -> Path:
        """兼容旧字段名：现在指向账号表。"""
        return self.accounts_file

    @property
    def token_cache(self) -> Path:
        """兼容旧字段名：现在是凭证缓存（token 或会话快照）。"""
        return self.credential_cache


def _norm_prefix(value) -> str:
    """规范化 URL 前缀。

    允许环境文件里写成 "/flow"、"flow"、"/flow/" 甚至 "flow/"，统一成 "/flow"；
    空值、"/" 一律归一成 ""，表示「无前缀」。
    """
    text = str(value or "").strip()
    if not text or text == "/":
        return ""
    return "/" + text.strip("/")


def load_settings(env: str = "dev", secrets_file: str = "secrets.env") -> Settings:
    loaded = load_secrets(ROOT / "config" / secrets_file)

    env_file = ROOT / "config" / "env" / f"{env}.yaml"
    if not env_file.exists():
        raise ConfigError(f"找不到环境配置: {env_file}")
    data = yaml.safe_load(env_file.read_text(encoding="utf-8")) or {}

    base_url = os.environ.get("BASE_URL") or data.get("base_url")
    if not base_url:
        raise ConfigError(f"{env_file} 缺少 base_url")
    if not loaded and not any(k.endswith("_USERNAME") or k.endswith("_SECRET") for k in os.environ):
        raise ConfigError(
            f"没有加载到任何凭据：请检查 config/{secrets_file} 是否存在，或改用环境变量注入"
        )

    return Settings(
        env=env,
        base_url=str(base_url).rstrip("/"),
        timeout=float(data.get("timeout", 10)),
        retries=int(data.get("retries", 2)),
        backoff=float(data.get("backoff", 0.3)),
        verify_ssl=bool(data.get("verify_ssl", False)),
        log_level=str(data.get("log_level", "INFO")),
        accounts_file=ROOT / str(data.get("accounts_file")
                                 or data.get("roles_file", "config/roles.yaml")),
        credential_cache=ROOT / "runtime" / f"credentials.{env}.json",
        persist_credentials=bool(data.get("persist_credentials", True)),
        # 环境变量 API_PREFIX 优先，便于 CI 里临时切换网关前缀
        api_prefix=_norm_prefix(os.environ.get("API_PREFIX") or data.get("api_prefix")),
    )


def load_accounts(path: Path) -> dict:
    """读取账号表，并把 ${VAR} 展开成真实凭据值。

    同时接受顶层键 `accounts:`（推荐）与 `roles:`（旧文件），便于平滑迁移。
    """
    if not path.exists():
        raise ConfigError(f"找不到账号定义文件: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accounts = raw.get("accounts") or raw.get("roles") or {}
    if not accounts:
        raise ConfigError(f"{path} 中没有定义任何账号")
    defaults = raw.get("defaults") or {}
    out = {}
    for name, spec in accounts.items():
        # 必须深合并：账号自己写 login.json 时不能把 defaults 里的 login.path 冲掉
        out[name] = _expand(_deep_merge(defaults, spec or {}))
    return out


#: 兼容旧调用名
load_roles = load_accounts


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 优先。override 里显式的 None 用来「取消继承」。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _deep_merge(base[key], value)
        else:
            merged[key] = value
    return merged


def _expand(node):
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, str) and "${" in node:
        return _resolve_refs(node)
    return node


def _resolve_refs(text: str) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        value = os.environ.get(key)
        if value is None:
            raise ConfigError(
                f"凭据缺失：环境变量 {key} 未定义。"
                f"请在 config/secrets.env 里补齐，或在 CI 中作为 Secret 注入。"
            )
        return value
    return _ENV_REF.sub(replace, text)
