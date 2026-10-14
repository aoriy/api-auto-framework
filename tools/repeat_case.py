#!/usr/bin/env python
"""重复执行**同一个用例** N 次，并统计成功 / 失败次数。

用法：
    export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
    PY="C:/Users/33032/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

    # 指定用例重复 1 万次（默认次数就是 10000）
    "$PY" tools/repeat_case.py --env dev --case cases.order_flow_case:OrderFlowCase

    # 按套件 + 序号挑用例，只跑 200 次并静音框架日志
    "$PY" tools/repeat_case.py --env dev --suite regression --index 1 -n 200 --quiet

    # 先看看套件里有哪些用例
    "$PY" tools/repeat_case.py --list --suite regression

设计要点：
  * 装配链路与 runner/main.py 完全同源（load_settings → TokenStore →
    SessionFactory → Context），所以「执行一次」的结果和跑套件时一模一样，
    唯一区别是把这一次塞进了 for 循环。
  * 账号客户端与凭证由 SessionFactory 缓存，循环 1 万次只登录一次，
    后续每次请求都命中进程内缓存 —— 统计出来的耗时是纯业务耗时。
  * 单次执行不抛异常（BaseCase.run() 内部已兜底），所以循环不会被中断，
    1 万次全跑完才汇总。
  * 失败明细按「步骤 + 归一化原因」聚合计数，否则 1 万行日志会把人淹死；
    同时保留前几条原始明细用于排查。

退出码：0 全部成功 / 1 存在失败 / 2 框架级错误（与 runner/main.py 一致）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import yaml  # noqa: E402

from config.settings import load_accounts, load_settings      # noqa: E402
from core.context import Context                              # noqa: E402
from core.exceptions import FrameworkError                    # noqa: E402
from core.logger import get_logger                            # noqa: E402
from core.session_factory import SessionFactory               # noqa: E402
from core.token_store import TokenStore                       # noqa: E402

#: 归一化时抹掉的部分：每次都不同的耗时、时间戳
_NOISE = (
    (re.compile(r"\d+(?:\.\d+)?ms"), "<ms>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<time>"),
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="repeat_case",
        description="重复执行单个用例 N 次并统计成败",
    )
    parser.add_argument("--env", default="dev", help="环境名，对应 config/env/<env>.yaml")
    parser.add_argument("--case", default="", help="用例，格式 '模块:类名'，如 cases.order_flow_case:OrderFlowCase")
    parser.add_argument("--suite", default="regression", help="套件名（配合 --list / --index 使用）")
    parser.add_argument("--index", type=int, default=None,
                        help="从套件里取第 N 个用例（从 0 开始），与 --case 二选一")
    parser.add_argument("-n", "--times", type=int, default=10000, help="重复次数，默认 10000")
    parser.add_argument("--list", action="store_true", help="只打印套件里的用例清单后退出")
    parser.add_argument("--quiet", action="store_true", help="静音框架日志，只留最终统计")
    parser.add_argument("--fail-fast", action="store_true", help="第一次失败就停止")
    parser.add_argument("--no-warmup", action="store_true",
                        help="跳过预热（预热那一次不计入统计，只为建立会话与连接池）")
    parser.add_argument("--keep-detail", type=int, default=5, help="保留多少条原始失败明细，默认 5")
    parser.add_argument("--progress-every", type=int, default=0,
                        help="每 N 次刷新一次进度，默认自动（约 20 次刷新）")
    parser.add_argument("--report", default="", help="把汇总结果写成 JSON 的路径（可选）")
    return parser.parse_args(argv)


def load_suite_cases(suite_name: str) -> list[str]:
    path = BASE / "suites" / f"{suite_name}.yaml"
    if not path.exists():
        raise SystemExit(f"找不到套件文件: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(x) for x in data.get("cases", [])]


def resolve_case(spec: str):
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise SystemExit(f"用例格式应为 '模块:类名'，当前: {spec}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"导入模块 {module_name} 失败: {exc}")
    cls = getattr(module, class_name, None)
    if cls is None:
        raise SystemExit(f"模块 {module_name} 里没有类 {class_name}")
    return cls


def pick_case(args) -> str:
    """--case 优先；否则用 --suite + --index 从套件里挑。"""
    if args.case:
        return args.case
    cases = load_suite_cases(args.suite)
    if args.index is None:
        raise SystemExit(
            f"必须用 --case 模块:类名 指定用例，或用 --index 从套件 '{args.suite}' 里挑。\n"
            + "\n".join(f"  --index {i}  {c}" for i, c in enumerate(cases))
        )
    if not 0 <= args.index < len(cases):
        raise SystemExit(f"--index 越界：套件 '{args.suite}' 只有 {len(cases)} 个用例")
    return cases[args.index]


def normalize(text: str, limit: int = 140) -> str:
    """把每次都会变的数字抹掉，否则同一种失败会被算成几千条。"""
    out = text or ""
    for pattern, repl in _NOISE:
        out = pattern.sub(repl, out)
    out = out.strip()
    return out if len(out) <= limit else out[:limit] + "..."


def fmt_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(p / 100 * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def print_progress(done: int, total: int, passed: int, failed: int, started: float) -> None:
    elapsed = time.perf_counter() - started
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    line = (f"  进度 {done}/{total}  ({done / total * 100:5.1f}%)  "
            f"成功 {passed}  失败 {failed}  {rate:6.1f} 次/s  已用 {fmt_seconds(elapsed)}"
            f"  预计剩余 {fmt_seconds(eta)}")
    sys.stderr.write("\r" + line.ljust(110))
    sys.stderr.flush()


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list:
        print(f"\n套件 '{args.suite}' 里的用例：")
        for i, entry in enumerate(load_suite_cases(args.suite)):
            print(f"  --index {i}   {entry}")
        print()
        return 0

    if args.times <= 0:
        raise SystemExit("--times 必须是正整数")

    case_spec = pick_case(args)
    settings = load_settings(args.env)
    log = get_logger("runner", "WARNING" if args.quiet else settings.log_level)
    accounts = load_accounts(settings.accounts_file)

    # 与 runner/main.py 同一套装配
    store = TokenStore(settings.credential_cache, persist=settings.persist_credentials)
    factory = SessionFactory(settings, accounts, store, logger=log)
    ctx = Context(factory, settings.env)
    case_cls = resolve_case(case_spec)

    quiet = args.quiet

    def say(text: str = "") -> None:
        if not quiet:
            print(text)

    print(f"\n{'=' * 68}")
    print(f"重复执行：{case_spec}")
    print(f"环境 {settings.env}   目标 {settings.base_url}{settings.api_prefix or ''}"
          f"   计划 {args.times} 次")
    print(f"{'=' * 68}")

    if not args.no_warmup:
        say("预热一次（建立会话与连接池，不计入统计）...")
        warm_started = time.perf_counter()
        warm = case_cls(ctx).run()
        say(f"  预热{'成功' if warm.ok else '失败'}，耗时 {warm.duration_ms}ms\n")

    durations: list[float] = []
    failures: Counter = Counter()
    samples: list[dict] = []
    passed = failed = 0
    stopped_early = False
    every = args.progress_every if args.progress_every > 0 else max(1, args.times // 20)

    started = time.perf_counter()
    for i in range(1, args.times + 1):
        try:
            result = case_cls(ctx).run()
        except BaseException as exc:                       # noqa: BLE001 循环级兜底
            failed += 1
            failures[f"<未捕获> {type(exc).__name__}: {exc}"] += 1
            if len(samples) < args.keep_detail:
                samples.append({"run": i, "step": "<case>",
                                "detail": f"{type(exc).__name__}: {exc}"})
            if args.fail_fast:
                stopped_early = True
                break
            continue

        durations.append(result.duration_ms)

        if result.ok:
            passed += 1
        else:
            failed += 1
            for step in result.steps:
                if step.ok:
                    continue
                key = f"{step.step} | {normalize(step.detail)}"
                failures[key] += 1
                if len(samples) < args.keep_detail:
                    samples.append({"run": i, "step": step.step, "detail": step.detail})
            if result.error:
                key = f"<用例级> | {normalize(result.error)}"
                failures[key] += 1
                if len(samples) < args.keep_detail:
                    samples.append({"run": i, "step": "<case>", "detail": result.error})

        if i % every == 0 or i == args.times:
            print_progress(i, args.times, passed, failed, started)

        if args.fail_fast and failed:
            stopped_early = True
            break

    wall = time.perf_counter() - started
    executed = passed + failed
    sys.stderr.write("\n")

    durations.sort()
    summary = {
        "env": settings.env,
        "base_url": settings.base_url,
        "case": case_spec,
        "total": executed,
        "planned": args.times,
        "stopped_early": stopped_early,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / executed * 100, 2) if executed else 0.0,
        "wall_seconds": round(wall, 2),
        "throughput_per_second": round(executed / wall, 2) if wall > 0 else 0.0,
        "duration_ms": {
            "avg": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "p50": round(percentile(durations, 50), 2),
            "p95": round(percentile(durations, 95), 2),
            "max": round(durations[-1], 2) if durations else 0.0,
            "min": round(durations[0], 2) if durations else 0.0,
        },
        "failure_reasons": [{"reason": k, "count": v} for k, v in failures.most_common()],
        "failure_samples": samples,
    }

    line = "-" * 68
    print(f"\n{'=' * 68}")
    print(f"用例 {case_spec} @ {settings.env}"
          f"{'   [提前停止]' if stopped_early else ''}")
    print(line)
    print(f"总执行次数 {executed}"
          f"{'（计划 %d，提前停止）' % args.times if stopped_early else ''}")
    print(f"成功次数   {passed}")
    print(f"失败次数   {failed}")
    if executed:
        print(f"成功率     {passed / executed * 100:.2f}%")
    print(line)
    print(f"总耗时 {fmt_seconds(wall)}   吞吐 {summary['throughput_per_second']} 次/s")
    if durations:
        d = summary["duration_ms"]
        print(f"单次耗时(ms)  平均 {d['avg']}   P50 {d['p50']}   P95 {d['p95']}"
              f"   最快 {d['min']}   最慢 {d['max']}")

    if failures:
        print(line)
        print(f"失败原因分布（共 {failed} 次失败，抹掉耗时差异后聚合）：")
        for key, count in failures.most_common(10):
            print(f"  x{count:<6} {key}")
        if len(failures) > 10:
            print(f"  ... 另有 {len(failures) - 10} 种不同原因，详见 JSON 报告")
        if samples:
            print(f"\n原始失败明细（前 {len(samples)} 条）：")
            for item in samples:
                print(f"  第 {item['run']} 次 | {item['step']} | {item['detail'][:160]}")
    print("=" * 68 + "\n")

    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = BASE / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"汇总报告: {report_path}\n")

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FrameworkError as exc:
        print(f"\n[框架级错误] {type(exc).__name__}: {exc}\n")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[已中断]\n")
        sys.exit(130)
