"""`probex` 产品 CLI（P0001.10.3）：**机器优先**的只读接口。

契约：

- 只读（status / inspect / query / report / explain）；**没有** buy / sell / order / set-risk / set-leverage；
- `--json` 时 stdout 只输出稳定 JSON（Agent 不需要解析终端文本）；
- stdout = 数据，stderr = diagnostics；
- 稳定退出码：0 ok / 2 参数错误 / 10 不可用 / 11 证据不足（UNKNOWN）/ 20 被 policy·readiness 阻塞 / 30 内部错误；
- 不实现任何业务逻辑：本模块只做 HTTP 读取与格式化，绝不直接读领域内部对象、绝不调用 Binance。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from cli.render import fact_rows, fact_text, list_text, rows

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 10
EXIT_UNKNOWN = 11
EXIT_BLOCKED = 20
EXIT_INTERNAL = 30

DEFAULT_API_URL = "http://127.0.0.1:8787"

#: command -> (endpoint, snapshot section)。section 为 None 表示整份 snapshot。
SLICE_COMMANDS: dict[str, tuple[str, str | None]] = {
    "status": ("/api/v1/status", "health"),
    "market": ("/api/v1/market", "market"),
    "prediction": ("/api/v1/prediction", "prediction"),
    "strategy": ("/api/v1/strategy", "strategy"),
    "risk": ("/api/v1/risk", "risk"),
    "orders": ("/api/v1/orders", "execution"),
    "portfolio": ("/api/v1/portfolio", "portfolio"),
    "readiness": ("/api/v1/readiness", "readiness"),
    "evidence": ("/api/v1/evidence", "evidence"),
    "snapshot": ("/api/v1/snapshot", None),
    "inspect": ("/api/v1/snapshot", None),
}
REPORT_PATH = "/api/v1/reports/run-summary"


class CliUnavailable(Exception):
    """API 不可用（连接失败 / 5xx / 非预期响应）。"""


def _opener(api_url: str):
    """loopback 绕过代理（本机 API 不应经过 HTTP 代理），其余走默认行为。"""
    host = urllib.parse.urlsplit(api_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def fetch(api_url: str, path: str, *, opener=None) -> dict:
    opener = opener or _opener(api_url)
    url = f"{api_url.rstrip('/')}{path}"
    try:
        with opener.open(url, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise CliUnavailable(f"{path} -> HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise CliUnavailable(f"{path} -> {type(error).__name__}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CliUnavailable(f"{path} -> invalid JSON") from error
    if not isinstance(payload, dict):
        raise CliUnavailable(f"{path} -> unexpected payload type")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probex", description="Probex product CLI (read-only)")
    parser.add_argument("--api-url", default=os.environ.get("PROBEX_API_URL", DEFAULT_API_URL),
                        help="Product API base URL (env PROBEX_API_URL)")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    for name in SLICE_COMMANDS:
        sub = subparsers.add_parser(name, help=f"read {name}")
        sub.add_argument("--json", action="store_true", help="emit stable JSON on stdout")
        if name == "orders":
            sub.add_argument("--active", action="store_true", help="only active orders")
        if name == "readiness":
            sub.add_argument("--explain", action="store_true", help="include evidence trace")
            sub.add_argument("--strict", action="store_true",
                             help="exit 20 when readiness is blocked (policy/readiness gate)")
        if name == "evidence":
            sub.add_argument("--decision-id", default=None, help="filter trace by decision identity")

    explain = subparsers.add_parser("explain", help="explain a decision / order")
    explain.add_argument("target", choices=["decision", "order"])
    explain.add_argument("identifier")
    explain.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report", help="read run reports")
    report.add_argument("target", choices=["run"])
    report.add_argument("identifier")
    report.add_argument("--format", choices=["json", "markdown"], default="json")
    report.add_argument("--json", action="store_true")
    return parser


def _emit(payload: dict, *, as_json: bool, stdout, stderr) -> None:
    if as_json:
        json.dump(payload, stdout, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        stdout.write("\n")
        return
    for key, value in payload.items():
        if isinstance(value, dict) and "known" in value:
            stdout.write(f"{key}: {fact_text(value)}\n")
        elif isinstance(value, dict):
            stdout.write(f"[{key}]\n{fact_rows(value)}\n")
        else:
            stdout.write(f"{key}: {value}\n")


def _facts_unknown(*facts: dict) -> bool:
    return all(isinstance(item, dict) and item.get("known") is False for item in facts)


def main(argv: Sequence[str] | None = None, *, stdout=None, stderr=None,
         opener=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_request:  # argparse 的 --help / 参数错误
        code = int(exit_request.code or 0)
        return EXIT_OK if code == 0 else EXIT_USAGE
    if not args.command:
        parser.print_help(file=stderr)
        return EXIT_USAGE

    api_url = args.api_url
    command = args.command
    try:
        if command == "report":
            path = f"{REPORT_PATH}?format={args.format}"
            if args.format == "markdown":
                payload = fetch(api_url, path, opener=opener)  # JSON 端点同时给出 markdown 提示
            else:
                payload = fetch(api_url, path, opener=opener)
            summary = payload.get("run_summary") or {}
            run = summary.get("run") or {}
            if args.identifier not in (run.get("run_id"), run.get("runtime", {}).get("runtime_id")):
                stderr.write(f"error: run {args.identifier!r} not found in the current run summary\n")
                return EXIT_UNKNOWN
            _emit(payload if args.json else {"run_id": run.get("run_id"), "run": run,
                                             "decision_counts": summary.get("decision_counts"),
                                             "order_counts": summary.get("order_counts")},
                  as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                stdout.write(f"markdown_hint: {api_url}{REPORT_PATH}?format=markdown\n")
            return EXIT_OK

        if command == "explain":
            evidence = fetch(api_url, "/api/v1/evidence", opener=opener).get("evidence", {})
            trace = [entry for entry in evidence.get("trace", [])
                     if args.identifier in json.dumps(entry, ensure_ascii=False)]
            if not trace:
                stderr.write(f"error: no evidence entry matches {args.target} {args.identifier!r}\n")
                return EXIT_UNKNOWN
            _emit({"target": args.target, "identifier": args.identifier, "trace": trace}
                  if args.json else {"matches": str(len(trace))},
                  as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                for entry in trace:
                    stdout.write(f"- {entry.get('stage')}: {entry.get('outcome')}\n")
            return EXIT_OK

        entry = SLICE_COMMANDS.get(command)
        if entry is None:
            stderr.write(f"error: unknown command {command!r}\n")
            return EXIT_USAGE
        path, section_name = entry
        payload = fetch(api_url, path, opener=opener)
        if section_name is None:
            _emit(payload, as_json=args.json, stdout=stdout, stderr=stderr)
        else:
            section = payload.get(section_name)
            if section is None:
                stderr.write(f"error: payload has no section {section_name!r}\n")
                return EXIT_INTERNAL
            if command == "orders" and args.active:
                orders = (section.get("active_orders") or [])
                _emit({"active_orders": orders} if args.json else {"active_orders": str(len(orders))},
                      as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json:
                    for order in orders:
                        stdout.write(f"- {order.get('client_order_id')} {order.get('side')} "
                                     f"{order.get('status')} decision={fact_text_json(order.get('decision_id'))}\n")
                return EXIT_OK
            if command == "readiness":
                blockers = section.get("reasons") or []
                status = section.get("status")
                if args.explain:
                    evidence = fetch(api_url, "/api/v1/evidence", opener=opener).get("evidence", {})
                    section = {**section, "trace": evidence.get("trace", [])}
                _emit(section if args.json else section, as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json and blockers:
                    stdout.write(f"blockers:\n{list_text(blockers)}\n")
                if isinstance(status, dict) and status.get("known") is False:
                    stderr.write("error: readiness status is UNKNOWN (insufficient evidence)\n")
                    return EXIT_UNKNOWN
                if args.strict and (blockers or (isinstance(status, dict) and status.get("value") != "live_ready")):
                    stderr.write("error: readiness is blocked (policy/readiness gate)\n")
                    return EXIT_BLOCKED
                return EXIT_OK
            if command == "prediction" and _facts_unknown(section.get("request_id", {})):
                _emit(section, as_json=args.json, stdout=stdout, stderr=stderr)
                stderr.write("error: insufficient evidence: no prediction record yet (UNKNOWN)\n")
                return EXIT_UNKNOWN
            _emit(section, as_json=args.json, stdout=stdout, stderr=stderr)
        return EXIT_OK
    except CliUnavailable as error:
        stderr.write(f"error: product API unavailable: {error}\n")
        return EXIT_UNAVAILABLE
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        stderr.write("interrupted\n")
        return EXIT_INTERNAL
    except Exception as error:  # noqa: BLE001 - 稳定退出码契约要求兜底
        stderr.write(f"error: internal failure: {type(error).__name__}: {error}\n")
        return EXIT_INTERNAL


def fact_text_json(value: object) -> str:
    if isinstance(value, dict) and "known" in value:
        return "UNKNOWN" if not value.get("known") else str(value.get("value"))
    return str(value)


def run() -> None:  # pragma: no cover - console entry
    raise SystemExit(main())


__all__ = ["EXIT_BLOCKED", "EXIT_INTERNAL", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_UNKNOWN", "EXIT_USAGE",
           "CliUnavailable", "build_parser", "fetch", "main", "run"]
