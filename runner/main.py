"""主程序：统一调度所有用例。

用法：
    python runner/main.py --env dev --suite regression
    python runner/main.py --env session --suite multi_account
    python runner/main.py --env session --list-accounts
    python runner/main.py --env session --fresh-token          # 忽略凭证缓存，强制重新登录

职责边界：
  * 装载配置、装配 SessionFactory、构建 Context
  * 按 suites/*.yaml 里声明的顺序依次执行用例类
  * 汇总结果、落 JSON 报告、用退出码表达成败（CI 友好）
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from config.settings import load_accounts, load_settings      # noqa: E402
from core.auth import available_types                         # noqa: E402
from core.context import Context                              # noqa: E402
from core.exceptions import FrameworkError                    # noqa: E402
from core.logger import get_logger                            # noqa: E402
from core.session_factory import SessionFactory               # noqa: E402
from core.token_store import TokenStore                       # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="runner",
        description="接口自动化测试框架主程序（面向页面 + 多账号）",
    )
    parser.add_argument("--env", default="dev", help="环境名，对应 config/env/<env>.yaml")
    parser.add_argument("--suite", default="regression", help="套件名，对应 suites/<suite>.yaml")
    parser.add_argument("--account", "--role", action="append", default=[], dest="accounts",
                        help="预热指定账号（可重复），例如 --account buyer_a --account ops_admin")
    parser.add_argument("--fresh-token", "--fresh-credentials", action="store_true",
                        dest="fresh_token", help="忽略凭证缓存，强制重新登录")
    parser.add_argument("--list-accounts", "--list-roles", action="store_true",
                        dest="list_accounts", help="只打印账号与鉴权方式后退出")
    return parser.parse_args(argv)


def load_suite(name: str) -> dict:
    path = BASE / "suites" / f"{name}.yaml"
    if not path.exists():
        raise SystemExit(f"找不到套件文件: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve_cases(suite: dict):
    pairs = []
    for entry in suite.get("cases", []):
        module_name, _, class_name = str(entry).partition(":")
        if not class_name:
            raise SystemExit(f"套件条目格式应为 '模块:类名'，当前: {entry}")
        module = importlib.import_module(module_name)
        pairs.append((entry, getattr(module, class_name)))
    return pairs


def print_accounts(accounts: dict) -> None:
    print("\n账号注册表：")
    for name, spec in accounts.items():
        auth = spec.get("auth") or {}
        role = spec.get("role") or "-"
        print(f"  {name:<12} role={role:<10} auth={auth.get('type', '-'):<10} "
              f"{spec.get('description', '')}")
    print(f"\n已支持的鉴权类型: {', '.join(available_types())}\n")


def build_report(settings, suite, results, duration_ms, store) -> dict:
    steps = [s for r in results for s in r.steps]
    return {
        "env": settings.env,
        "base_url": settings.base_url,
        "api_prefix": settings.api_prefix,
        "suite": suite.get("name", "unnamed"),
        "duration_ms": duration_ms,
        "summary": {
            "cases": len(results),
            "cases_passed": sum(1 for r in results if r.ok),
            "steps": len(steps),
            "passed": sum(1 for s in steps if s.ok),
            "failed": sum(1 for s in steps if not s.ok),
        },
        "credentials": [
            {
                "account": t.account,
                "env": t.env,
                "kind": t.kind,
                "preview": (t.value[:8] if t.value
                            else ",".join(sorted(t.cookies))[:32]),
                "cookie_count": len(t.cookies),
                "remaining_seconds": None if t.remaining == float("inf") else round(t.remaining),
            }
            for t in store.all()
        ],
        "cases": [r.to_dict() for r in results],
    }


def print_summary(report: dict, report_path: Path) -> None:
    summary = report["summary"]
    line = "-" * 68
    print("\n" + "=" * 68)
    print(f"套件「{report['suite']}」 @ {report['env']}   总耗时 {report['duration_ms']}ms")
    print(line)
    for case in report["cases"]:
        flag = "PASS" if case["ok"] else "FAIL"
        total = case["passed"] + case["failed"]
        print(f"  [{flag}] {case['name']:<24} 步骤 {case['passed']}/{total}   "
              f"{case['duration_ms']}ms")
        for step in case["steps"]:
            if not step["ok"]:
                print(f"         x {step['step']}: {step['detail']}")
        if case["error"]:
            print(f"         ! 用例级异常: {case['error']}")
    print(line)
    print(f"步骤合计 {summary['passed']} 通过 / {summary['failed']} 失败 / 共 {summary['steps']}")
    print(f"用例合计 {summary['cases_passed']} 通过 / "
          f"{summary['cases'] - summary['cases_passed']} 失败 / 共 {summary['cases']}")
    if report["credentials"]:
        print("凭证缓存（由框架按账号托管，用例完全无感知）：")
        for cred in report["credentials"]:
            left = "无期限" if cred["remaining_seconds"] is None else f"{cred['remaining_seconds']}s"
            if cred["kind"] == "cookie":
                preview = f"会话 {cred['cookie_count']} 个 Cookie"
            else:
                preview = f"{cred['preview']}..."
            print(f"  {cred['account']:<12} {preview:<26} 剩余 {left}")
    print(f"报告文件: {report_path}")
    print("=" * 68 + "\n")


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.env)
    log = get_logger("runner", settings.log_level)
    accounts = load_accounts(settings.accounts_file)

    if args.list_accounts:
        print_accounts(accounts)
        return 0

    print_accounts(accounts)
    log.info("环境 %s   目标 %s", settings.env, settings.base_url)
    if settings.api_prefix:
        log.info("页面公共前缀 %s   （config/env/%s.yaml 的 api_prefix）",
                 settings.api_prefix, settings.env)
    else:
        # 未声明 prefix 的页面对象会直接拼出 /xxx，表现为大面积 404。
        # 这是把前缀上移到环境配置后唯一需要当心的地方，所以启动就提醒。
        log.warning("页面公共前缀为空：config/env/%s.yaml 里可能漏配了 api_prefix，"
                    "未声明 prefix 的页面对象将直接拼接 /xxx", settings.env)
    if not settings.persist_credentials:
        log.info("凭证不落盘：只在本进程内复用")

    store = TokenStore(settings.credential_cache, persist=settings.persist_credentials)
    if args.fresh_token:
        store.clear(settings.env)
        log.info("已清空环境 %s 的凭证缓存", settings.env)

    factory = SessionFactory(settings, accounts, store, logger=log)

    for account in args.accounts:
        factory.login(account, force=args.fresh_token)

    ctx = Context(factory, settings.env)
    suite = load_suite(args.suite)
    pairs = resolve_cases(suite)
    log.info("套件「%s」共 %d 个用例，开始执行", suite.get("name", args.suite), len(pairs))

    results = []
    started = time.perf_counter()
    for entry, case_cls in pairs:
        log.info(">>> %s", entry)
        results.append(case_cls(ctx).run())
    duration = round((time.perf_counter() - started) * 1000, 2)

    report = build_report(settings, suite, results, duration, store)
    report_path = BASE / "reports" / f"report-{settings.env}-{args.suite}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print_summary(report, report_path)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FrameworkError as exc:
        print(f"\n[框架级错误] {type(exc).__name__}: {exc}\n")
        sys.exit(2)
