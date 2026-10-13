"""单接口联调工具（hit.py）—— 只打一个接口，把过程和结果全摊开。

为什么需要它：
  runner/main.py 的调度单位是「套件」，套件里最小的执行粒度是「用例类」，
  而一个用例类往往是整条业务流程。新增一个接口时，为了验证「这一个接口
  到底通没通」，跑整条流程既慢又吵（上游一挂，你要的那个接口根本没发出去）。

  本工具直接复用框架已有的装配链路（settings → SessionFactory → Context →
  页面对象），把「一个接口」拉出来单独打一发，并打印全部联调所需信息。

它复用了什么（这点很重要：不是另起一套）：
  * 同一份 config/env/<env>.yaml 与账号表  → 环境、前缀、账号完全一致
  * 同一个 SessionFactory / TokenStore      → 凭证复用同一份缓存，不会多登录
  * 同一个 HttpClient                        → 鉴权、401 自愈、重试行为与用例内一致
  * 同一个页面对象                           → 调的就是用例将来会调的那个方法

典型用法：
  # 0) 先看有什么可用（接口清单 + 默认账号 + 前缀 + HTTP 方法 + 路径）
  python tools/hit.py --env session --list

  # 1) 打一个已经封装好的页面方法（最常用）
  python tools/hit.py --env session --api ApplyPageApi.list_applications \
      --account test1 --kw page=1

  # 2) 换账号看权限（同一接口、不同账号，验证越权是否真的被拦）
  python tools/hit.py --env session --api FlowApi.sre_review --account test34 \
      --arg c-001 --expect-status 200
  python tools/hit.py --env session --api FlowApi.sre_review --account test1 \
      --arg c-001 --expect-status 403

  # 3) 不带凭证打（验证未登录是否被拦）
  python tools/hit.py --env session --api FlowApi.me --account test1 --no-auth \
      --expect-status 401

  # 4) 接口还没封装成页面方法时，先打裸路径探路
  python tools/hit.py --env session --raw GET /changes --account test1 \
      --prefix /flow --expect data.items
  python tools/hit.py --env session --raw POST /changes --account test1 \
      --prefix /flow --json '{"title":"联调验证"}'

  # 5) 记录真实响应结构，用于写页面对象
  python tools/hit.py --env session --api ApplyPageApi.list_applications \
      --account test1 --save runtime/probe/apply_list.json

Git Bash 用户注意：
  MSYS 会把以 / 开头的参数当路径改写（--prefix /flow 会变成 <Git目录>/flow）。
  本工具能自动还原；若还原失败会直接报错并提示加 MSYS_NO_PATHCONV=1。
  想彻底避免，也可以写成不带前导斜杠的形式：--prefix flow

退出码：0 = 全部断言通过；1 = 有断言失败；2 = 框架级错误（配置/账号/网络）。
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from apis.base_api import BaseApi                                    # noqa: E402
from config.settings import load_accounts, load_settings             # noqa: E402
from core.context import Context                                     # noqa: E402
from core.exceptions import FrameworkError                           # noqa: E402
from core.http_client import HttpClient                              # noqa: E402
from core.logger import get_logger                                   # noqa: E402
from core.response import ApiResponse                                # noqa: E402
from core.session_factory import SessionFactory                      # noqa: E402
from core.token_store import TokenStore                              # noqa: E402

LINE = "-" * 72


def die(message: str) -> None:
    """用法 / 配置类错误：明确报出并退出码 2（与「断言失败=1」区分开，CI 好判）。"""
    print(f"\n[用法或配置错误] {message}\n")
    sys.exit(2)

#: BaseApi 自身的请求方法，不算「业务接口」，列清单时排除
_PLUMBING = {"get", "post", "put", "patch", "delete", "as_account", "as_role"}
#: 匹配「谁在发请求」：self.get(...) / self.as_account("x").post(...) / self.as_role("x").get(...)
_VERB = re.compile(
    r"(?:self|self\.as_(?:account|role)\([^)]*\))\.(get|post|put|patch|delete)\(\s*"
    r"(?:self\.ctx\.interpolate\()?"
)
#: 紧随其后的第一个字符串字面量就是路径（可能是 f-string）
_PATH = re.compile(r"""f?"([^"]*)"|f?'([^']*)'""")
#: 只做账号切换、不发请求的辅助方法
_SWITCH = re.compile(r"self\.as_(?:account|role)\(")


# ======================================================================
# 接口发现
# ======================================================================
def discover() -> dict[str, type[BaseApi]]:
    """扫 apis/ 下所有 BaseApi 子类。"""
    import apis

    found: dict[str, type[BaseApi]] = {}
    for mod in pkgutil.iter_modules(apis.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"apis.{mod.name}")
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, BaseApi) and obj is not BaseApi
                    and obj.__module__ == module.__name__):
                found[name] = obj
    return found


def endpoints(cls: type[BaseApi]) -> list[tuple[str, str, str, str]]:
    """静态提取页面类里每个方法对应的 (方法名, HTTP 动词, 路径, 签名)。

    页面类里的路径有三种写法，都要认出来：
        self.post("/cart/orders", ...)
        self.get(self.ctx.interpolate(f"/cart/orders/{order_id}"))     # 包在 interpolate 里
        self.as_account("ops_admin").post(f"/cart/orders/{oid}/approve")  # 换账号发
    只做源码级提取用于打印清单，不参与实际请求。
    """
    out = []
    for name in sorted(dir(cls)):
        if name.startswith("_") or name in _PLUMBING:
            continue
        fn = getattr(cls, name)
        if not callable(fn):
            continue
        try:
            src = inspect.getsource(fn)
            sig = str(inspect.signature(fn)).replace("(self, ", "(").replace("(self)", "()")
        except (OSError, TypeError):
            continue
        seen: list[tuple[str, str]] = []
        for match in _VERB.finditer(src):
            window = src[match.end():match.end() + 200]
            hit = _PATH.search(window)
            if not hit:
                continue
            path = hit.group(1) if hit.group(1) is not None else hit.group(2)
            item = (match.group(1).upper(), path)
            if item not in seen:
                seen.append(item)
        for verb, path in seen:
            out.append((name, verb, path, sig))
        if not seen and _SWITCH.search(src):
            # 例如 UserApi.as_public()：只换账号并返回页面对象，本身不发请求
            out.append((name, "HELPER", "", sig))
    return out


def print_inventory(settings, apis_map: dict[str, type[BaseApi]]) -> None:
    print(f"\n环境 {settings.env}   base_url={settings.base_url}   "
          f"api_prefix={settings.api_prefix or '(空)'}\n")
    for name, cls in sorted(apis_map.items()):
        declared = cls.prefix
        prefix = settings.api_prefix if declared is None else declared
        principal = cls.account or cls.role
        print(f"{name}   默认账号={principal}   前缀={prefix if prefix else '(无)'}")
        eps = endpoints(cls)
        if not eps:
            print("    (未发现接口方法)")
        for method_name, verb, path, sig in eps:
            if verb == "HELPER":
                print(f"    [辅助] {method_name}{sig}   —— 只换账号，不发请求，不能用本工具直接调")
            else:
                print(f"    {verb:<6} {prefix}{path}")
                print(f"           {method_name}{sig}")
        print()
    total_eps = sum(1 for c in apis_map.values() for e in endpoints(c) if e[1] != "HELPER")
    print(f"共 {len(apis_map)} 个页面类 / {total_eps} 个接口方法\n")


# ======================================================================
# 取值解析
# ======================================================================
def parse_value(text: str):
    """CLI 值解析：能当 JSON 解的就解（数字/布尔/数组/对象），否则当字符串。"""
    try:
        return json.loads(text)
    except ValueError:
        return text


def kv_pairs(items: list[str], sep: str) -> dict:
    out = {}
    for item in items:
        key, found, raw = item.partition(sep)
        if not found:
            die(f"参数格式应为 key{sep}value，当前: {item}")
        out[key.strip()] = parse_value(raw.strip())
    return out


# ----------------------------------------------------------------------
# Git Bash（MSYS）会把以 / 开头的命令行参数当路径改写：
#     --prefix /flow   →  <Git 安装目录>/flow
# 结果是 URL 拼成 http://host/E:/soft/.../PortableGit/flow/changes 这种鬼样子。
# 这里把它还原回来；真的还原不了就明确报错，而不是让你对着 404 发呆。
# ----------------------------------------------------------------------
_MSYS_FALLBACK = re.compile(r"^[A-Za-z]:/.*?/(?:PortableGit|Git|mingw64|usr)(/.*)$")
_msys_root_cache: str | None = None


def _msys_root() -> str:
    global _msys_root_cache
    if _msys_root_cache is None:
        _msys_root_cache = ""
        try:
            done = subprocess.run(["cygpath", "-m", "/"], capture_output=True,
                                  text=True, timeout=3)
            if done.returncode == 0:
                _msys_root_cache = done.stdout.strip().rstrip("/")
        except (OSError, subprocess.SubprocessError):
            pass
    return _msys_root_cache


def fix_msys_path(value: str) -> str:
    """把被 Git Bash 改写过的路径还原成原来的绝对路径。"""
    norm = value.replace("\\", "/")
    root = _msys_root()
    if root and norm.startswith(root + "/"):
        return "/" + norm[len(root) + 1:]
    hit = _MSYS_FALLBACK.match(norm)
    if hit:
        return hit.group(1)
    return value


def clean_path(value: str, label: str) -> str:
    fixed = fix_msys_path(value)
    if ":/" in fixed:
        die(
            f"{label} 的值 '{value}' 看起来被 Git Bash 改写过了，且无法自动还原。\n"
            f"  两种解法：\n"
            f"    1) 命令前加 MSYS_NO_PATHCONV=1（推荐）\n"
            f"    2) 不写前导斜杠，例如把 /flow 写成 flow（本工具会自动补 /）"
        )
    return fixed


def normalize_prefix(value: str | None) -> str:
    """前缀规范化：flow → /flow；已带 / 的原样；被 MSYS 改写过的还原。"""
    if not value:
        return ""
    fixed = clean_path(value, "--prefix")
    return fixed if fixed.startswith("/") else "/" + fixed


# ======================================================================
# 装配（复用框架主链路，不另起一套）
# ======================================================================
def build_context(args):
    settings = load_settings(args.env)
    accounts = load_accounts(settings.accounts_file)
    log = get_logger("hit", args.log_level or settings.log_level)
    store = TokenStore(settings.credential_cache, persist=settings.persist_credentials)
    if args.fresh_token:
        store.clear(settings.env)
        log.info("已清空环境 %s 的凭证缓存", settings.env)
    factory = SessionFactory(settings, accounts, store, logger=log)
    return settings, accounts, Context(factory, settings.env)


def pick_account(accounts: dict, wanted: str | None, fallback: str | None) -> str | None:
    chosen = wanted or fallback
    if chosen and chosen not in accounts:
        available = ", ".join(sorted(accounts))
        die(
            f"账号 '{chosen}' 不在环境账号表里。\n"
            f"当前可用的账号：{available}\n"
            f"（提示：页面类的默认账号可能属于另一个环境，用 --account 显式指定）"
        )
    return chosen


class AnonymousContext:
    """--no-auth 专用上下文：全新连接、不挂任何凭证。

    为什么不能直接给正常客户端传 auth=False？
      因为 Cookie 鉴权下凭证存在 Session 的 CookieJar 里，requests 会自动带上，
      auth=False 只跳过「注入请求头」这一步，带不走已经存在的 Cookie ——
      结果就是你以为在测未登录，实际上还是登录态（实测会返回 200 而不是 401）。
      所以这里直接用一条全新的 requests.Session（HttpClient 内部创建），
      保证请求上真的一个凭证都没有。
    """

    def __init__(self, settings):
        self._prefix = getattr(settings, "api_prefix", "") or ""
        self._client = HttpClient(
            base_url=settings.base_url,
            account="anonymous",
            timeout=settings.timeout,
            retries=settings.retries,
            backoff=settings.backoff,
            verify=settings.verify_ssl,
        )

    @property
    def api_prefix(self) -> str:
        return self._prefix

    def client(self, account=None):
        return self._client

    def interpolate(self, value):
        # 匿名探针不参与跨步骤变量传递，原样返回
        return value


# ======================================================================
# 展示
# ======================================================================
def pretty(text: str, limit: int) -> str:
    if not text:
        return "(空响应体)"
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    if limit and len(text) > limit:
        return text[:limit] + f"\n... (已截断，完整长度 {len(text)} 字符，用 --max-body 0 看全部)"
    return text


def show_request(label: str, resp, args) -> None:
    kw = resp.request_kwargs or {}
    print(LINE)
    print(f"[{label}] 请求")
    print(f"  {resp.method} {resp.url}")
    print(f"  发起账号 : {resp.account}")
    if kw.get("params"):
        print(f"  query    : {json.dumps(kw['params'], ensure_ascii=False)}")
    if kw.get("json"):
        print(f"  json body: {json.dumps(kw['json'], ensure_ascii=False)}")
    if kw.get("data"):
        print(f"  form body: {kw['data']}")
    if args.header:
        print(f"  附加头   : {json.dumps(kv_pairs(args.header, ':'), ensure_ascii=False)}")
    print(f"[{label}] 响应")
    ctype = resp.headers.get("Content-Type", "")
    print(f"  HTTP {resp.status_code}   {resp.elapsed_ms}ms   {ctype}")
    if args.show_cookies:
        raw_cookie = resp.headers.get("Set-Cookie", "")
        print(f"  Set-Cookie: {raw_cookie or '(无)'}")
    print("  body:")
    for line in pretty(resp.text, args.max_body).splitlines():
        print(f"    {line}")


# ======================================================================
# 断言
# ======================================================================
def verify(resp, args) -> list[str]:
    problems: list[str] = []
    if args.expect_status is not None and resp.status_code != args.expect_status:
        problems.append(f"状态码期望 {args.expect_status}，实际 {resp.status_code}")
    for path in args.expect:
        try:
            value = resp.require(path)
        except Exception:
            problems.append(f"字段 '{path}' 不存在")
            continue
        if value in (None, "", [], {}):
            problems.append(f"字段 '{path}' 为空值: {value!r}")
    for item in args.expect_eq:
        path, sep, want = item.partition("=")
        if not sep:
            die(f"--expect-eq 格式应为 PATH=VALUE，当前: {item}")
        try:
            actual = resp.require(path.strip())
        except Exception:
            problems.append(f"字段 '{path.strip()}' 不存在")
            continue
        if str(actual) != want.strip():
            problems.append(f"字段 '{path.strip()}' 期望 {want.strip()!r}，实际 {actual!r}")
    return problems


# ======================================================================
# 调用
# ======================================================================
def call_method(ctx, accounts, cls, method: str, args) -> list:
    target = getattr(cls, method, None)
    if target is None or not callable(target):
        die(f"{cls.__name__} 上没有方法 '{method}'")
    account = pick_account(accounts, args.account, cls.account or cls.role)
    api = cls(ctx, account=account) if account else cls(ctx)
    fn = getattr(api, method)

    pos = [parse_value(a) for a in args.arg]
    kw = kv_pairs(args.kw, "=")
    print(LINE)
    print(f"调用 {cls.__name__}.{method}()  账号={api.principal}  前缀={api.prefix or '/'}")
    if pos or kw:
        print(f"  入参: args={pos} kwargs={json.dumps(kw, ensure_ascii=False)}")
    responses = []
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n  --- 第 {i + 1}/{args.repeat} 次 ---")
        result = fn(*pos, **kw)
        # 页面方法可能返回单个响应，也可能返回响应列表（多步接口）
        items = result if isinstance(result, (list, tuple)) else [result]
        for resp in items:
            if not isinstance(resp, ApiResponse):
                die(
                    f"{cls.__name__}.{method}() 没有返回 ApiResponse，而是 {type(resp).__name__}。\n"
                    f"说明它只是辅助方法（比如 as_public 只换账号），不是可直接调用的接口。"
                )
            show_request(method, resp, args)
            responses.append(resp)
    return responses


def call_raw(ctx, accounts, args) -> list:
    method, path = args.raw
    if not args.account:
        die("--raw 必须配 --account（不知道用哪个账号的会话就没法发请求）")
    account = pick_account(accounts, args.account, None)
    client = ctx.client(account)
    # 直接走 HttpClient：绕过页面对象的 prefix 逻辑，路径完全自己说了算
    prefix = normalize_prefix(args.prefix)
    path = clean_path(path, "--raw 的 PATH")
    if prefix and not path.startswith("/"):
        # --prefix flow --raw GET changes 也要能拼成 /flow/changes
        path = "/" + path
    full_path = f"{prefix}{path}"
    headers = kv_pairs(args.header, ":") if args.header else None
    payload_kwargs = {
        "params": json.loads(args.params) if args.params else None,
        "json": json.loads(args.json) if args.json else None,
        "data": kv_pairs(args.data, "=") if args.data else None,
        "headers": headers,
        "auth": not args.no_auth,
    }
    print(LINE)
    print(f"裸打接口 {method.upper()} {client.base_url}{full_path}  账号={account}")
    responses = []
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n  --- 第 {i + 1}/{args.repeat} 次 ---")
        resp = client.request(method.upper(), full_path, **payload_kwargs)
        show_request("raw", resp, args)
        responses.append(resp)
    return responses


# ======================================================================
# CLI
# ======================================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="hit",
        description="单接口联调工具：只打一个接口，打印全链路信息并断言",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--env", default="dev", help="环境名，对应 config/env/<env>.yaml（默认 dev）")
    p.add_argument("--account", help="指定账号，覆盖页面类的默认账号")
    p.add_argument("--list", action="store_true", help="打印所有页面类与接口方法后退出")
    p.add_argument("--list-accounts", action="store_true", help="打印账号注册表后退出")
    p.add_argument("--fresh-token", action="store_true", help="忽略凭证缓存，强制重新登录")

    p.add_argument("--api", help="要调用的接口，格式 页面类.方法名，例如 ApplyPageApi.list_applications")
    p.add_argument("--arg", action="append", default=[], help="位置参数（可重复，能当 JSON 解就当 JSON）")
    p.add_argument("--kw", action="append", default=[], help="关键字参数 key=value（可重复）")

    p.add_argument("--raw", nargs=2, metavar=("METHOD", "PATH"),
                   help="不经过页面对象，直接打裸路径，例如 --raw POST /changes")
    p.add_argument("--prefix", help="--raw 时拼接在路径前面的前缀，例如 /flow")
    p.add_argument("--params", help="--raw 的 query 参数（JSON）")
    p.add_argument("--json", dest="json", help="--raw 的 JSON 请求体（JSON）")
    p.add_argument("--data", action="append", default=[], help="--raw 的表单字段 key=value（可重复）")
    p.add_argument("--header", action="append", default=[], help="附加请求头 key:value（可重复）")
    p.add_argument("--no-auth", action="store_true", help="不带凭证发送，用于验证未登录拦截")

    p.add_argument("--expect-status", type=int, help="期望状态码，不符即失败")
    p.add_argument("--expect", action="append", default=[],
                   help="断言响应体某路径存在且非空（点号路径，可重复），例如 --expect data.items")
    p.add_argument("--expect-eq", action="append", default=[],
                   help="断言响应体某路径等于期望值（可重复），例如 --expect-eq data.role=creator")

    p.add_argument("--repeat", type=int, default=1, help="重复发送次数，用于观察缓存/幂等")
    p.add_argument("--save", help="把本次响应存成 JSON 文件，便于照着写页面对象")
    p.add_argument("--max-body", type=int, default=2000, help="响应体打印上限字符数，0 表示不截断")
    p.add_argument("--show-cookies", action="store_true", help="打印响应里的 Set-Cookie")
    p.add_argument("--log-level", help="覆盖日志级别，调链路时用 DEBUG")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    settings, accounts, ctx = build_context(args)

    if args.no_auth and (args.api or args.raw):
        # 必须是「换一条全新连接」，不能靠 auth=False —— 见 AnonymousContext 的说明
        ctx = AnonymousContext(settings)
        print(LINE)
        print("⚠️ --no-auth：使用全新匿名连接（Cookie 与 Token 一个都不带）")
        print(LINE)

    if args.list_accounts:
        print("\n账号注册表：")
        for name, spec in accounts.items():
            print(f"  {name:<12} role={spec.get('role', '-'):<14} "
                  f"auth={(spec.get('auth') or {}).get('type', '-'):<8} "
                  f"{spec.get('description', '')}")
        print()
        return 0

    if args.list:
        print_inventory(settings, discover())
        return 0

    if args.raw:
        responses = call_raw(ctx, accounts, args)
    elif args.api:
        cls_name, sep, method = args.api.partition(".")
        if not sep:
            die("--api 格式应为 页面类.方法名，例如 ApplyPageApi.list_applications")
        apis_map = discover()
        if cls_name not in apis_map:
            die(f"未找到页面类 '{cls_name}'。可用：{', '.join(sorted(apis_map))}")
        responses = call_method(ctx, accounts, apis_map[cls_name], method, args)
    else:
        die("请指定 --api 类.方法 或 --raw METHOD PATH（用 --list 看清单）")

    problems: list[str] = []
    for resp in responses:
        problems.extend(verify(resp, args))

    print(LINE)
    if problems:
        print(f"❌ 断言失败 {len(problems)} 项：")
        for item in problems:
            print(f"    - {item}")
    else:
        print(f"✅ 通过（{len(responses)} 个响应）"
              + (f"  最后一次 HTTP {responses[-1].status_code}" if responses else ""))

    if args.save and responses:
        last = responses[-1]
        out = Path(args.save) if Path(args.save).is_absolute() else BASE / args.save
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "env": settings.env,
            "method": last.method,
            "url": last.url,
            "account": last.account,
            "status": last.status_code,
            "elapsed_ms": last.elapsed_ms,
            "headers": last.headers,
            "body": last.json(default=None),
            "raw_text": last.text,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"响应已存: {out}")
    print(LINE)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FrameworkError as exc:
        print(f"\n[框架级错误] {type(exc).__name__}: {exc}\n")
        sys.exit(2)
