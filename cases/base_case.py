"""用例基类：一个类 = 一条业务流程。

「主程序统一调度、按顺序执行」这条要求，在这里落地：
  * 用例类里所有以 test_ 开头的方法都是测试步骤；
  * 步骤执行顺序 = 方法名字典序，所以用 test_01_ / test_02_ 前缀控制先后；
  * 生命周期：setup_case() → test_01_* → test_02_* → ... → teardown_case()
  * 每一步的结果都记进 CaseResult，单个步骤失败不会中断整条流程，
    这样一次运行就能看到全貌，而不是修一个跑一次。
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Callable

from apis.base_api import BaseApi
from core.context import Context
from core.response import ApiResponse


@dataclass
class StepResult:
    case: str
    step: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"case": self.case, "step": self.step, "ok": self.ok,
                "detail": self.detail, "duration_ms": self.duration_ms}


@dataclass
class CaseResult:
    name: str
    steps: list = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and all(s.ok for s in self.steps)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s.ok)

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "error": self.error,
                "duration_ms": self.duration_ms, "passed": self.passed,
                "failed": self.failed, "steps": [s.to_dict() for s in self.steps]}


class BaseCase:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.result = CaseResult(name=type(self).__name__)
        self._apis: dict[tuple, BaseApi] = {}

    # ---------------- 页面对象（带缓存） ----------------
    def api(self, api_cls, account: str | None = None, role: str | None = None) -> BaseApi:
        """取页面对象。同一个用例里「同类型 + 同账号」只构造一次。

        多账号写法：
            self.shop_a = self.api(ShopApi)                      # 用页面类默认账号
            self.shop_b = self.api(ShopApi, account="buyer_b")   # 换成账号 B
        两个实例各自持有账号 B / 默认账号的客户端，互不影响。
        """
        chosen = account or role
        key = (api_cls, chosen)
        if key not in self._apis:
            self._apis[key] = (api_cls(self.ctx, account=chosen) if chosen
                               else api_cls(self.ctx))
        return self._apis[key]

    # ---------------- 结果记录 ----------------
    def mark(self, step: str, ok: bool = True, detail: str = "") -> bool:
        """记录一个非 HTTP 的步骤结果（比如「取到了 product_id」）。"""
        self._record(step, ok, detail)
        return ok

    def check(self, step: str, response: ApiResponse, expect_status: int = 200,
              expect: Callable | None = None, detail: str = "") -> bool:
        """记录一次接口调用：校验状态码，可选再跑一组断言。"""
        ok = response.status_code == expect_status
        info = f"HTTP {response.status_code} / {response.elapsed_ms}ms / account={response.account}"
        if ok and expect is not None:
            try:
                expect(response)
                info += " / 断言通过"
            except AssertionError as exc:
                ok = False
                info += f" / 断言失败: {exc}"
        elif not ok:
            info += f" / 期望 {expect_status}，响应 {response.text[:120]}"
        if detail:
            info = f"{detail} | {info}"
        self._record(step, ok, info)
        return ok

    def _record(self, step: str, ok: bool, detail: str) -> None:
        self.result.steps.append(
            StepResult(case=self.result.name, step=step, ok=bool(ok), detail=detail)
        )

    # ---------------- 生命周期钩子 ----------------
    def setup_case(self) -> None:
        """用例开始前准备页面对象、预置数据。"""

    def teardown_case(self) -> None:
        """用例结束后清理数据。"""

    # ---------------- 执行 ----------------
    @classmethod
    def step_names(cls) -> list[str]:
        """按字典序返回所有 test_ 开头的步骤方法。"""
        names = [n for n, _ in inspect.getmembers(cls, inspect.isfunction)
                 if n.startswith("test_")]
        return sorted(names)

    def run(self) -> CaseResult:
        started = time.perf_counter()
        try:
            self.setup_case()
            for name in self.step_names():
                step_started = time.perf_counter()
                first_index = len(self.result.steps)
                try:
                    getattr(self, name)()
                except Exception as exc:                     # noqa: BLE001 用例级兜底
                    self._record(name, False, f"未捕获异常 {type(exc).__name__}: {exc}")
                for step in self.result.steps[first_index:]:
                    step.duration_ms = round((time.perf_counter() - step_started) * 1000, 2)
            self.teardown_case()
        except Exception as exc:                             # noqa: BLE001 setup/teardown 兜底
            self.result.error = f"{type(exc).__name__}: {exc}"
            if not self.result.steps:
                self._record("<case>", False, self.result.error)
        finally:
            self.result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return self.result
