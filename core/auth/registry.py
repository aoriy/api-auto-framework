"""鉴权策略注册表。

扩展新鉴权方式只需要两步：
  1. 继承 AuthStrategy 实现 apply()
  2. 打上 @register 装饰器（type = "xxx"）
之后 roles.yaml 里写 type: xxx 就能用，框架其它代码一行都不用改。
"""
from __future__ import annotations

from core.auth.base import AuthStrategy
from core.exceptions import ConfigError

_REGISTRY: dict[str, type[AuthStrategy]] = {}


def register(cls: type[AuthStrategy]) -> type[AuthStrategy]:
    """登记一个鉴权策略。"""
    key = getattr(cls, "type", None)
    if not key or key == "base":
        raise ConfigError(f"{cls.__name__} 必须声明一个非 base 的 type")
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ConfigError(f"鉴权类型 '{key}' 已被 {_REGISTRY[key].__name__} 占用")
    _REGISTRY[key] = cls
    return cls


def build_strategy(spec: dict | None) -> AuthStrategy:
    """按 roles.yaml 的 auth 段实例化策略。"""
    if not isinstance(spec, dict) or not spec.get("type"):
        raise ConfigError(f"角色配置的 auth 段必须包含 type 字段，当前值: {spec!r}")
    key = spec["type"]
    cls = _REGISTRY.get(key)
    if cls is None:
        raise ConfigError(f"未知鉴权方式 '{key}'，已注册: {sorted(_REGISTRY)}")
    return cls(spec)


def available_types() -> list[str]:
    return sorted(_REGISTRY)
