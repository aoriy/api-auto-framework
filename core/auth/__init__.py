"""鉴权子包入口。导入本包即完成所有内置策略的注册。"""
from core.auth.base import AuthStrategy
from core.auth.registry import available_types, build_strategy, register
from core.auth import strategies  # noqa: F401  触发 @register 注册

__all__ = ["AuthStrategy", "build_strategy", "register", "available_types", "strategies"]
