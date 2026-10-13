"""框架统一异常体系：所有可预期错误都从这里派生，便于 runner 统一兜底。"""
from __future__ import annotations


class FrameworkError(Exception):
    """框架内所有可预期异常的基类。"""


class ConfigError(FrameworkError):
    """配置缺失或非法：环境文件、roles.yaml、凭据变量。"""


class RoleNotDefined(ConfigError):
    """请求了账号表里不存在的账号。"""


#: 语义更准确的别名（账号是登录单位，角色只是账号的属性）
AccountNotDefined = RoleNotDefined


class AuthError(FrameworkError):
    """登录失败、token 提取失败、鉴权信息缺失。"""


class ApiError(FrameworkError):
    """请求层异常：连接失败、超时、重试耗尽。"""


class PathNotFound(FrameworkError):
    """在响应体里按点号路径取值失败。"""
